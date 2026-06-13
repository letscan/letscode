"""Event subscribers: MessageSubscriber and CliOutputSubscriber."""

import json
import sys
from pathlib import Path
from typing import Any

from .tools._display import format_call, format_result, format_stream_line


# ---------------------------------------------------------------------------
# Result persistence (shared with LogSubscriber in events.py)
# ---------------------------------------------------------------------------

RESULT_THRESHOLD = 32 * 1024  # 32KB


def persist_result(log_stem: Path, tool_call_id: str, result: str) -> str:
    """Persist a large tool result to disk. Returns the reference message."""
    results_dir = log_stem.parent / (log_stem.stem + "_results")
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{tool_call_id}.txt"
    result_path.write_text(result, encoding="utf-8")

    preview_limit = 2000
    if len(result) > preview_limit:
        truncated = result[:preview_limit]
        last_nl = truncated.rfind("\n")
        cut = last_nl if last_nl > preview_limit // 2 else preview_limit
        preview = result[:cut]
    else:
        preview = result

    size_kb = len(result) / 1024
    return (
        f"<persisted-output>\n"
        f"Output too large ({size_kb:.1f} KB). "
        f"Full output saved to: {result_path}\n\n"
        f"Preview:\n{preview}\n"
        f"{'...' if len(result) > preview_limit else ''}\n"
        f"</persisted-output>"
    )


# ---------------------------------------------------------------------------
# Result resolution (for feed replay — handles all formats)
# ---------------------------------------------------------------------------


def _resolve_result(data: dict) -> str:
    """Get tool result from event data, reading from file if externalized."""
    if "rawOutput" in data:
        return data["rawOutput"]
    if "result" in data:
        return data["result"]
    result_file = data.get("result_file")
    if result_file:
        try:
            return Path(result_file).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return data.get("result_summary", "")
    return ""


# ---------------------------------------------------------------------------
# MessageSubscriber
# ---------------------------------------------------------------------------


class MessageSubscriber:
    """Builds the messages list from events. Works for both live and replay.

    Usage:
        sub = MessageSubscriber(log_stem=path)  # live: enables result persistence
        # or
        sub = MessageSubscriber()               # replay: no persistence needed
        for event in events:
            sub(event["type"], event["data"])
        sub.flush()
        messages = sub.messages
    """

    def __init__(self, log_stem: Path | None = None):
        self.messages: list[dict[str, Any]] = []
        self._log_stem = log_stem  # for result persistence (live only)

        # Accumulation state
        self._text_parts: list[str] = []
        self._pending_tools: dict[str, dict] = {}  # tid -> {id, name, arguments, input, result?}
        self._tool_order: list[str] = []
        self._extra_after_tool: dict[str, list[dict]] = {}  # tid -> [user msgs]

    def __call__(self, event_type: str, data: dict) -> None:
        handler = getattr(self, f"_on_{event_type.replace('/', '_')}", None)
        if handler:
            handler(data)

    def flush(self) -> None:
        self._flush_turn()

    # -- event handlers --

    def _on_prompt(self, data: dict) -> None:
        self._flush_turn()
        if isinstance(data, list):
            text = "".join(b.get("text", "") for b in data if b.get("type") == "text")
        else:
            text = ""
        self.messages.append({"role": "user", "content": text})

    def _on_session_prompt(self, data: dict) -> None:
        # Legacy format
        self._flush_turn()
        blocks = data.get("prompt", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        self.messages.append({"role": "user", "content": text})

    def _on_agent_message_chunk(self, data: dict) -> None:
        if self._tool_order:
            self._flush_turn()
        # Flat format: {"type": "text", "text": "..."}
        if "text" in data and "type" in data:
            self._text_parts.append(data.get("text", ""))
        else:
            # Nested legacy format
            self._text_parts.append(data.get("content", {}).get("text", ""))

    def _on_tool_call(self, data: dict) -> None:
        tid = data.get("toolCallId", "")
        inp = data.get("rawInput", data.get("input", {}))
        name = data.get("toolName", "")
        self._pending_tools[tid] = {
            "id": tid,
            "name": name,
            "arguments": json.dumps(inp, ensure_ascii=False) if isinstance(inp, dict) else str(inp),
            "input": inp,
            "stream_parts": [],
        }
        self._tool_order.append(tid)

    def _on_tool_call_update(self, data: dict) -> None:
        tid = data.get("toolCallId", "")
        status = data.get("status", "")

        if tid not in self._pending_tools:
            return

        if not status:
            # Process output (streaming chunk) — accumulate for reconstruction
            chunk = data.get("rawOutput", "")
            if chunk:
                self._pending_tools[tid]["stream_parts"].append(chunk)
            return

        if status in ("completed", "failed"):
            # Final event: prefer rawOutput, fall back to accumulated stream
            result = _resolve_result(data)
            if not result:
                parts = self._pending_tools[tid].get("stream_parts", [])
                result = "\n".join(parts) if parts else ""
            if status == "failed" and not result.startswith("<error>"):
                result = f"<error>{result}</error>" if result else "<error>failed</error>"
            self._pending_tools[tid]["result"] = result

    def _on_user_message(self, data: dict) -> None:
        self._add_extra_user_message(data)

    def _on_user_message_chunk(self, data: dict) -> None:
        self._add_extra_user_message(data)

    def _add_extra_user_message(self, data: dict) -> None:
        if isinstance(data, dict) and "text" in data and "type" in data:
            text = data.get("text", "")
        else:
            text = data.get("content", {}).get("text", "")
        if text and self._tool_order:
            last_tid = self._tool_order[-1]
            self._extra_after_tool.setdefault(last_tid, []).append(
                {"role": "user", "content": text}
            )

    # -- turn management --

    def _flush_turn(self) -> None:
        if not self._text_parts and not self._tool_order:
            return

        full_text = "".join(self._text_parts)
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": full_text or None,
        }
        if self._tool_order:
            assistant_msg["tool_calls"] = [
                {
                    "id": self._pending_tools[tid]["id"],
                    "type": "function",
                    "function": {
                        "name": self._pending_tools[tid]["name"],
                        "arguments": self._pending_tools[tid]["arguments"],
                    },
                }
                for tid in self._tool_order
            ]
        self.messages.append(assistant_msg)

        for tid in self._tool_order:
            tool = self._pending_tools[tid]
            name = tool["name"]
            result = tool.get("result", "")

            # Persist large results (live mode only)
            if self._log_stem and len(result) > RESULT_THRESHOLD:
                result = persist_result(self._log_stem, tid, result)

            # Skill expansion: replace tool content with summary
            if name == "Skill" and not result.startswith("<error>"):
                skill_name = tool.get("input", {}).get("skill", "")
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": f"Launching skill: {skill_name}",
                })
                # User message with full skill content is handled by
                # user_message_chunk events → _extra_after_tool
            else:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": result,
                })

            for msg in self._extra_after_tool.get(tid, []):
                self.messages.append(msg)

        self._text_parts = []
        self._pending_tools = {}
        self._tool_order = []
        self._extra_after_tool = {}


# ---------------------------------------------------------------------------
# CliOutputSubscriber
# ---------------------------------------------------------------------------


class CliOutputSubscriber:
    """Handles all CLI output: text to stdout, tool details to stderr.

    Always registered (unless --event-stream mode). The verbose flag
    controls whether tool call/result details are printed to stderr.
    """

    def __init__(self, verbose: bool = False):
        self._verbose = verbose
        self._streamed: set[str] = set()
        self._tool_info: dict[str, dict] = {}  # tid -> {name, args}

    def __call__(self, event_type: str, data: dict) -> None:
        if event_type == "agent_message_chunk":
            self._on_agent_message_chunk(data)
        elif event_type == "tool_call":
            self._on_tool_call(data)
        elif event_type == "tool_call_update":
            self._on_tool_call_update(data)

    def _on_agent_message_chunk(self, data: dict) -> None:
        # Always write text to stdout — this is the main LLM output
        if "text" in data and "type" in data:
            text = data.get("text", "")
        else:
            text = data.get("content", {}).get("text", "")
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _on_tool_call(self, data: dict) -> None:
        tid = data.get("toolCallId", "")
        name = data.get("toolName", "")
        args = data.get("rawInput", {})
        self._tool_info[tid] = {"name": name, "args": args}
        if self._verbose:
            print(format_call(name, args), file=sys.stderr)

    def _on_tool_call_update(self, data: dict) -> None:
        tid = data.get("toolCallId", "")
        status = data.get("status", "")
        raw_output = data.get("rawOutput")

        if not status and raw_output is not None:
            # Process output (streaming chunk)
            self._streamed.add(tid)
            if self._verbose:
                print(format_stream_line(raw_output), file=sys.stderr)
        elif status in ("completed", "failed"):
            if self._verbose:
                success = status == "completed"
                if tid in self._streamed:
                    # Streaming output already shown line-by-line; just mark done
                    from .tools._display import _status, _dim
                    print(_dim(_status(success) + "done"), file=sys.stderr)
                else:
                    info = self._tool_info.get(tid, {})
                    name = info.get("name", "")
                    args = info.get("args", {})
                    result = raw_output or ""
                    print(format_result(name, result, success, args), file=sys.stderr)
