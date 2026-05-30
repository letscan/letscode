"""Slash command registry and built-in handlers for ACP sessions."""

import logging
import re
from dataclasses import dataclass, field

import acp.helpers as h
from acp.schema import AvailableCommand, AvailableCommandsUpdate

from ..feed_util import (
    extract_conversation_text,
    extract_skill_activations,
    last_agent_text,
    read_events,
    split_turns,
    write_events,
)

logger = logging.getLogger("letscode-acp")


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: object | None = None
    is_skill: bool = False


@dataclass
class CommandResult:
    message: str


class SlashCommandRegistry:
    """Registry for slash commands available in ACP sessions."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, name: str, description: str, handler, *, is_skill: bool = False) -> None:
        self._commands[name] = SlashCommand(name=name, description=description, handler=handler, is_skill=is_skill)

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def dispatch(self, name: str, *args, **kwargs) -> CommandResult:
        cmd = self._commands.get(name)
        if cmd is None or cmd.handler is None:
            return CommandResult(message=f"未知命令: /{name}")
        return cmd.handler(*args, **kwargs)

    def to_acp_update(self) -> AvailableCommandsUpdate:
        commands = []
        for c in self._commands.values():
            desc = f"(Skill) {c.description}" if c.is_skill else c.description
            commands.append(AvailableCommand(
                name=c.name,
                description=desc,
            ))
        return AvailableCommandsUpdate(
            session_update="available_commands_update",
            availableCommands=commands,
        )


def parse_slash_command(blocks: list[dict]) -> tuple[str | None, str | None]:
    """Extract slash command from the first text content block.

    Returns (command_name, rest_of_text) or (None, None).
    """
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            m = re.match(r"^/(\w+)(?:\s+(.*))?$", text, re.DOTALL)
            if m:
                return m.group(1), m.group(2)
            break
    return None, None


# ── Built-in command handlers ──


def _handle_new(session, args: str | None = None, **kwargs) -> CommandResult:
    """Clear context and start fresh."""
    log_path = getattr(session, "log_path", None)
    if log_path:
        write_events(log_path, [])
    return CommandResult(message="已开启全新的上下文。")


def _handle_compact(session, args: str | None = None, **kwargs) -> CommandResult:
    """Compress context with an LLM-generated structured summary."""
    log_path = getattr(session, "log_path", None)
    if not log_path:
        return CommandResult(message="没有可压缩的上下文。")

    events = read_events(log_path)
    if not events:
        return CommandResult(message="没有可压缩的上下文。")

    turns = split_turns(events)
    if len(turns) <= 1:
        return CommandResult(message="上下文过短，无需压缩。")

    # Keep the last turn intact; summarize the rest
    keep_count = 1
    turns_to_keep = turns[-keep_count:]
    turns_to_summarize = turns[:-keep_count]
    summarize_events = [ev for turn in turns_to_summarize for ev in turn]

    # Extract skill activations that must survive compaction
    skill_events = extract_skill_activations(summarize_events)

    conversation_text = extract_conversation_text(summarize_events)

    if not conversation_text.strip():
        return CommandResult(message="上下文过短，无需压缩。")

    # Try LLM summarization
    config = kwargs.get("config")
    summary = _try_llm_summarize(config, conversation_text)

    # Build preserved events: skill activations (kept intact) + summary + recent turns
    if summary is None:
        # Fallback: just keep the last turn + skill activations
        kept_events = skill_events + [ev for turn in turns_to_keep for ev in turn]
        write_events(log_path, kept_events)
        removed_chars = len(conversation_text)
        return CommandResult(message=f"已压缩上下文（移除约 {removed_chars} 字符）。")

    # Build compacted log: skill activations + summary + kept turns
    summary_event = {
        "type": "agent_message_chunk",
        "data": {
            "content": {"type": "text", "text": f"[上下文摘要]\n{summary}"},
        },
    }
    kept_events = skill_events + [summary_event] + [ev for turn in turns_to_keep for ev in turn]
    write_events(log_path, kept_events)

    return CommandResult(message=f"已压缩上下文。摘要:\n{summary[:200]}")


def _handle_undo(session, args: str | None = None, **kwargs) -> CommandResult:
    """Roll back the last turn and show the previous agent message."""
    log_path = getattr(session, "log_path", None)
    if not log_path:
        return CommandResult(message="没有可回退的上下文。")

    events = read_events(log_path)
    if not events:
        return CommandResult(message="没有可回退的上下文。")

    turns = split_turns(events)
    if len(turns) <= 1:
        return CommandResult(message="已经是最早的上下文，无法回退。")

    # Remove the last turn
    removed_turn = turns[-1]
    kept_turns = turns[:-1]

    # Get the last agent text from the removed turn for display
    agent_text = last_agent_text(removed_turn)

    kept_events = [ev for turn in kept_turns for ev in turn]
    write_events(log_path, kept_events)

    if agent_text:
        return CommandResult(message=f"已回退到：{agent_text}")
    return CommandResult(message="已回退上一轮操作。")


# ── LLM Summarization ──

_COMPACT_SYSTEM_PROMPT = """You are a conversation summarizer. Produce a concise structured summary of the conversation that preserves the most important context for continuing the work. Use the following sections:

1. **主要任务**: What was the user trying to accomplish?
2. **当前进展**: What has been done so far? List concrete completed steps.
3. **关键文件**: Which files were read, edited, or created?
4. **重要结论**: Key decisions, findings, or constraints discovered during the conversation.
5. **待解决问题**: What issues or questions are still unresolved?
6. **上下文提示**: Any other context needed to continue (e.g., branch names, PR numbers, error details).

Keep the summary concise but comprehensive. Write in the same language as the conversation."""

_COMPACT_USER_PROMPT = """Please summarize the following conversation, preserving the most important context:

{conversation}"""


def _try_llm_summarize(config, conversation_text: str) -> str | None:
    """Attempt to summarize via LLM. Returns summary text or None on failure."""
    if config is None:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=config.api_key or "dummy",
            base_url=config.base_url,
        )

        user_msg = _COMPACT_USER_PROMPT.format(conversation=conversation_text[:60000])

        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=4000,
            stream=False,
        )

        summary = response.choices[0].message.content or ""
        # Strip <analysis> tags if the model wraps its output
        summary = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL).strip()
        return summary if summary else None

    except Exception:
        logger.debug("LLM summarization failed", exc_info=True)
        return None


# ── Registry factory ──


def create_builtin_registry() -> SlashCommandRegistry:
    """Create a registry with the built-in slash commands."""
    registry = SlashCommandRegistry()
    registry.register("new", "清空上下文，开始新对话", _handle_new)
    registry.register("compact", "压缩上下文，生成结构化摘要", _handle_compact)
    registry.register("undo", "回退上一轮操作", _handle_undo)
    return registry


def register_skills(registry: SlashCommandRegistry, cwd: str) -> None:
    """Discover skills for cwd and register them as slash commands (no handler)."""
    from ..tools.skill import get_skill_list

    for skill in get_skill_list(cwd):
        name = skill["name"]
        desc = skill.get("description", name)
        registry.register(name, desc, handler=None, is_skill=True)
