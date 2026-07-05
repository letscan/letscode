"""Convert a Claude Code session transcript into a letscode session feed.

Claude Code stores each session as a JSONL file under
``~/.claude/projects/<project>/<session-uuid>.jsonl``. Each line is one record
(``type: user | assistant | system | attachment | ...``) with ``message.content``
as either a plain string (real user prompt) or a list of content blocks
(text, thinking, tool_use, tool_result, image, ...).

This converter walks the transcript and emits letscode feed events:

- real user prompts      -> ``prompt``
- compact continuations  -> ``user_message_chunk`` (role=user on replay)
- assistant text blocks  -> ``agent_message_chunk``
- assistant tool_use     -> ``tool_call``
- user tool_result       -> ``tool_call_update`` (status=completed|failed)
- everything else        -> skipped, but recorded in the ConvertReport

The output is a valid letscode feed (``load_feed`` reconstructs a legal OpenAI
messages list). Subagent transcripts (``<session>/subagents/agent-*.jsonl``)
are NOT expanded — the Agent tool_result already carries the final answer
inline, matching letscode's black-box subagent semantics.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..feed_util import write_events


# Marker prefixes that identify Claude-Code synthetic user-content records
# (slash-command echoes, local-command stdout). These are not real user turns.
_NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

# A user content string starting with this is a post-compact continuation
# summary (Claude Code injects it after /compact). Map it to user_message_chunk
# so replay yields role=user, matching letscode's own compact output.
_COMPACT_CONTINUATION_PREFIX = "This session is being continued"

# Tool-result content extraction: CC tool_result.content is either a string or
# a list of blocks; we collapse to a single text string.


@dataclass
class ConvertReport:
    """Tracks Claude-Code features encountered during a conversion.

    The fields double as the data source for the analysis markdown report:
    each counts a category of CC input that was either mapped or skipped.
    """

    cc_path: str = ""
    total_lines: int = 0
    converted_events: int = 0

    # Successfully mapped
    user_prompts: int = 0                  # -> prompt events
    compact_continuations: int = 0         # -> user_message_chunk events
    agent_text_blocks: int = 0             # -> agent_message_chunk events
    tool_use_blocks: int = 0               # -> tool_call events
    tool_results: int = 0                  # -> tool_call_update events
    is_error_results: int = 0              # subset of tool_results, status=failed

    # Skipped — CC features letscode feed does not represent
    thinking_blocks_dropped: int = 0       # assistant thinking blocks
    skipped_user_markers: int = 0          # <command-name>/<local-command-*>
    skipped_types: Counter = field(default_factory=Counter)  # type -> count
    skipped_user_subtypes: Counter = field(default_factory=Counter)  # block type -> count (user)
    skipped_assistant_subtypes: Counter = field(default_factory=Counter)  # block type -> count (assistant)
    orphan_tool_results: int = 0           # tool_result with no matching tool_use
    image_blocks_dropped: int = 0          # image/document blocks in user content

    # Misc observations
    tool_result_string_content: int = 0    # tool_result.content was a bare string
    tool_result_list_content: int = 0      # tool_result.content was a block list
    agent_tool_calls: int = 0             # tool_use with name=="Agent" (subagents not expanded)
    sidechain_entries: int = 0            # records with isSidechain truthy


def convert_cc_session(cc_path: str, out_path: str) -> ConvertReport:
    """Convert a Claude Code session jsonl to a letscode session feed jsonl.

    Args:
        cc_path: path to the CC ``<session>.jsonl`` file.
        out_path: path to write the letscode feed (overwrites).

    Returns:
        A :class:`ConvertReport` summarizing what was mapped and what was
        skipped. The caller can render it to markdown via
        :func:`letscode.importers.report.render_report_md`.
    """
    report = ConvertReport(cc_path=str(cc_path))
    events: list[dict] = []
    # tool_use.id -> {name, input}; used to pair the later tool_result.
    pending_tool_uses: dict[str, dict] = {}
    init_emitted = False

    with open(cc_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            report.total_lines += 1

            # CC subagent transcripts live in separate files; the main file
            # should have none, but guard defensively.
            if _truthy(rec.get("isSidechain")):
                report.sidechain_entries += 1
                continue

            if not init_emitted:
                events.append(_make_init(rec))
                init_emitted = True

            rtype = rec.get("type")
            if rtype == "user":
                _handle_user(rec, events, pending_tool_uses, report)
            elif rtype == "assistant":
                _handle_assistant(rec, events, pending_tool_uses, report)
            else:
                # system / attachment / mode / permission-mode / ai-title /
                # last-prompt / agent-name / file-history-snapshot / queue-operation
                report.skipped_types[rtype or "<empty>"] += 1

    report.converted_events = len(events)
    write_events(out_path, events)
    return report


# ---------------------------------------------------------------------------
# Init synthesis
# ---------------------------------------------------------------------------


def _make_init(rec: dict) -> dict:
    """Synthesize a letscode init event from the first CC record.

    Model is unknown from the transcript (CC doesn't record per-message
    model), so a placeholder is used; replay callers override with --model.
    """
    cwd = rec.get("cwd") or ""
    version = rec.get("version") or "unknown"
    return {
        "type": "init",
        "timestamp": rec.get("timestamp") or _now_iso(),
        "data": {
            "agent": "claude-code (imported)",
            "version": version,
            "model": "claude-unknown",
            "cwd": cwd,
            "maxTokens": 0,
            "maxTurns": 0,
            "preset": "default",
            "sandbox": False,
            "tools": [],
        },
    }


# ---------------------------------------------------------------------------
# User record handling
# ---------------------------------------------------------------------------


def _handle_user(rec: dict, events: list[dict],
                 pending_tool_uses: dict[str, dict], report: ConvertReport) -> None:
    msg = rec.get("message") or {}
    content = msg.get("content")
    ts = rec.get("timestamp") or _now_iso()

    if isinstance(content, str):
        _handle_user_string(content, ts, events, report)
        return

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                _handle_tool_result(block, ts, events, pending_tool_uses, report)
            elif btype == "text":
                # Rare: a user turn carrying a free-text block. Treat as prompt.
                text = block.get("text", "")
                _handle_user_string(text, ts, events, report)
            elif btype in ("image", "document"):
                report.image_blocks_dropped += 1
            else:
                report.skipped_user_subtypes[btype or "<empty>"] += 1
        return

    # Unexpected content shape
    report.skipped_user_subtypes["<non-str-non-list>"] += 1


def _handle_user_string(text: str, ts: str, events: list[dict],
                        report: ConvertReport) -> None:
    text = text or ""
    stripped = text.lstrip()
    if any(stripped.startswith(p) for p in _NOISE_PREFIXES):
        report.skipped_user_markers += 1
        return
    if stripped.startswith(_COMPACT_CONTINUATION_PREFIX):
        # Post-compact summary injected by CC. Emit as user_message_chunk so
        # replay yields role=user (matching letscode's own compact product).
        events.append({
            "type": "user_message_chunk",
            "timestamp": ts,
            "data": {"type": "text", "text": text},
        })
        report.compact_continuations += 1
        return
    events.append({
        "type": "prompt",
        "timestamp": ts,
        "data": [{"type": "text", "text": text}],
    })
    report.user_prompts += 1


# ---------------------------------------------------------------------------
# Assistant record handling
# ---------------------------------------------------------------------------


def _handle_assistant(rec: dict, events: list[dict],
                      pending_tool_uses: dict[str, dict], report: ConvertReport) -> None:
    msg = rec.get("message") or {}
    content = msg.get("content")
    ts = rec.get("timestamp") or _now_iso()

    if not isinstance(content, list):
        # Defensive: assistant content is normally a block list.
        report.skipped_assistant_subtypes["<non-list>"] += 1
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            events.append({
                "type": "agent_message_chunk",
                "timestamp": ts,
                "data": {"type": "text", "text": text},
            })
            report.agent_text_blocks += 1
        elif btype == "thinking":
            # letscode thoughts are streaming-only and not replayed; drop.
            report.thinking_blocks_dropped += 1
        elif btype == "tool_use":
            _handle_tool_use(block, ts, events, pending_tool_uses, report)
        else:
            report.skipped_assistant_subtypes[btype or "<empty>"] += 1


def _handle_tool_use(block: dict, ts: str, events: list[dict],
                     pending_tool_uses: dict[str, dict], report: ConvertReport) -> None:
    tool_id = block.get("id") or ""
    name = block.get("name") or ""
    inp = block.get("input") or {}
    pending_tool_uses[tool_id] = {"name": name, "input": inp}
    events.append({
        "type": "tool_call",
        "timestamp": ts,
        "data": {
            "toolCallId": tool_id,
            "toolName": name,
            "rawInput": inp,
        },
    })
    report.tool_use_blocks += 1
    if name == "Agent":
        report.agent_tool_calls += 1


def _handle_tool_result(block: dict, ts: str, events: list[dict],
                        pending_tool_uses: dict[str, dict], report: ConvertReport) -> None:
    tool_use_id = block.get("tool_use_id") or ""
    is_error = bool(block.get("is_error"))
    content = block.get("content")

    # Track content shape for the report.
    if isinstance(content, str):
        report.tool_result_string_content += 1
    elif isinstance(content, list):
        report.tool_result_list_content += 1

    text = _extract_result_text(content)

    if tool_use_id not in pending_tool_uses:
        # Orphan result (no preceding tool_use). Skip but record.
        report.orphan_tool_results += 1
        return

    status = "failed" if is_error else "completed"
    if is_error and text and not text.startswith("<error>"):
        text = f"<error>{text}</error>"
        report.is_error_results += 1
    elif is_error:
        report.is_error_results += 1

    events.append({
        "type": "tool_call_update",
        "timestamp": ts,
        "data": {
            "toolCallId": tool_use_id,
            "status": status,
            "rawOutput": text,
        },
    })
    report.tool_results += 1
    # Once paired, the tool_use is resolved.
    pending_tool_uses.pop(tool_use_id, None)


def _extract_result_text(content) -> str:
    """Flatten a CC tool_result.content (string or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truthy(v) -> bool:
    """CC writes booleans as both actual bools and string 'True'/'true'."""
    return v is True or (isinstance(v, str) and v.lower() == "true")


def _now_iso() -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
