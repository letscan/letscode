"""Agent loop — LLM call → tool execution → result feedback cycle."""

import os
import sys
from typing import Any

from openai import OpenAI

from .config import ModelConfig
from .events import get_emitter, RESULT_THRESHOLD
from .mcp import get_manager
from .stream import consume_stream
from .tools import TOOL_DEFINITIONS, _call_summary, _result_summary
from .tools.runner import ToolRunner
from .tools._types import ToolResult


def _blocks_to_text(blocks: list[dict]) -> str:
    """Convert structured content blocks to plain text for the LLM."""
    parts: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "resource_link":
            name = b.get("name", "")
            uri = b.get("uri", "")
            parts.append(f"[@{name}]({uri})" if name else uri)
        elif t == "resource":
            text = b.get("resource", {}).get("text")
            parts.append(text if text else b.get("resource", {}).get("uri", ""))
        elif t == "image":
            parts.append(f"[image:{b.get('mime_type')}]")
    return "\n".join(parts)


def _process_tool_result(
    result: str, tool_id: str, tool_name: str, args: dict,
) -> tuple[str, list[dict]]:
    """Post-process a raw tool result: persist if large, expand if skill."""
    emitter = get_emitter()
    extra_messages: list[dict] = []

    if len(result) > RESULT_THRESHOLD and emitter:
        result = emitter.persist_result(result, tool_id)

    if tool_name == "Skill" and not result.startswith("<error>"):
        skill_name = args.get("skill", "")
        extra_messages.append({"role": "user", "content": result})
        result = f"Launching skill: {skill_name}"

    return result, extra_messages


async def run_agent(
    prompt_blocks: list[dict],
    system_prompt: str,
    config: ModelConfig,
    max_turns: int | None = None,
    feed_path: str | None = None,
    tool_runner: ToolRunner | None = None,
) -> int:
    """Run the agent loop until the LLM stops making tool calls.

    Returns exit code: 0 for success, 1 for error.
    """
    emitter = get_emitter()
    mcp = get_manager()
    tools = tool_runner or ToolRunner([], {})

    # --- Setup ---
    client = OpenAI(
        api_key=config.api_key or "dummy",
        base_url=config.base_url,
    )
    all_tools = tools.definitions
    tool_names = [t["function"]["name"] for t in all_tools]

    # Build messages
    prompt_text = _blocks_to_text(prompt_blocks)
    if feed_path:
        from .feed import load_feed
        _, history = load_feed(feed_path)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ] + history + [
            {"role": "user", "content": prompt_text},
        ]
    else:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text},
        ]

    # Emit init + prompt
    if emitter:
        rules = tools.rules
        rules_dict = {
            "allowRead": rules.allow_read,
            "denyRead": rules.deny_read,
            "allowWrite": rules.allow_write,
            "denyWrite": rules.deny_write,
            "allowCmd": rules.allow_cmd,
            "denyCmd": rules.deny_cmd,
        }
        emitter.emit_init(
            model=config.model, cwd=os.getcwd(), max_tokens=config.max_tokens,
            max_turns=max_turns or 0, preset=config.preset, sandbox=config.sandbox,
            tools=tool_names, rules=rules_dict,
        )
        emitter.emit_prompt(prompt_text, prompt_blocks=prompt_blocks)

    # --- Loop ---
    turn = 0
    had_error = False

    while True:
        if max_turns is not None and turn >= max_turns:
            print(f"\n[Reached max turns limit: {max_turns}]", file=sys.stderr)
            break

        turn += 1
        if emitter:
            emitter.set_turns(turn)

        # 1. LLM call
        try:
            on_line = emitter.on_text_line if emitter else None
            stream_result = consume_stream(
                client, config.model, messages, config.max_tokens,
                tools=all_tools, on_line=on_line,
            )
        except Exception as e:
            print(f"\nAPI error: {e}", file=sys.stderr)
            if emitter:
                emitter.emit_error(str(e), code="api_error", recoverable=False)
            had_error = True
            break

        text_content = stream_result.text_content
        tool_calls = stream_result.tool_calls

        if not tool_calls:
            if not text_content and emitter:
                emitter.emit_agent_message_chunk("(no response)")
            break

        if text_content and not emitter:
            sys.stdout.write("\n")

        # 2. Assistant message
        messages.append({
            "role": "assistant",
            "content": text_content or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        # 3. Tool execution via ToolRunner
        for tc in tool_calls:
            tool_name = tc.name
            tool_id = tc.id
            args = {}

            if emitter:
                import json
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                emitter.emit_tool_call(tool_id, tool_name, args)
                emitter.emit_tool_update(tool_id, "in_progress")

            if config.verbose:
                print(_call_summary(tool_name, args), file=sys.stderr)

            async for event in tools.execute(tool_name, tc.arguments):
                processed, extras = _process_tool_result(
                    event.content, tool_id, tool_name, args,
                )

                if config.verbose:
                    print(f"  <- {tool_name}: {_result_summary(tool_name, event.content)}", file=sys.stderr)

                if emitter:
                    status = "completed" if event.success else "failed"
                    emitter.emit_tool_update(tool_id, status, raw_output=processed)
                    for msg in extras:
                        emitter.emit_user_message_chunk(msg["content"])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": processed,
                })
                for msg in extras:
                    messages.append(msg)

    # --- Session end ---
    if emitter:
        stop_reason = "max_turn_requests" if (max_turns is not None and turn >= max_turns) else "end_turn"
        emitter.on_session_end(stop_reason)

    return 1 if had_error else 0
