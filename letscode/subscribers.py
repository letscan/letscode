"""Event subscribers: MessageSubscriber and CliOutputSubscriber."""

import json
import sys
from pathlib import Path
from typing import Any

from .tools._display import format_call, format_result


# ---------------------------------------------------------------------------
# Result persistence (shared with LogSubscriber in events.py)
# ---------------------------------------------------------------------------

RESULT_THRESHOLD = 32 * 1024  # 32KB


# ---------------------------------------------------------------------------
# StreamBuffer — accumulates streaming records into lines
# ---------------------------------------------------------------------------

class StreamBuffer:
    """Accumulates streaming records into lines, handling \\r and \\n.

    - "\\n" separator commits the current line and starts a new one.
    - "\\r" separator overwrites the current line (progress-bar behavior).

    all_lines: every line including the in-progress current line.
    preview: head/tail window for display — first n lines, an omitted
             placeholder, then the last n lines; or all lines if short.
             Same rule for streaming and final display.
    merged: full text joined by newlines, for persistence/replay.
    """

    def __init__(self, head_tail: int = 5):
        self._committed: list[str] = []
        self._current: str = ""
        self._at_line_start: bool = True
        self._n = head_tail

    def feed(self, content: str, separator: str) -> None:
        if self._at_line_start:
            self._current = content
        else:
            self._current += content

        if separator == "\r":
            self._at_line_start = True
        elif separator == "\n":
            self._committed.append(self._current)
            self._current = ""
            self._at_line_start = True

    @property
    def all_lines(self) -> list[str]:
        lines = list(self._committed)
        if self._current:
            lines.append(self._current)
        return lines

    def preview(self) -> tuple[list[str], int]:
        """Return (display_lines, omitted_count) using a head/tail window.

        - <= 2n lines: all lines, omitted 0.
        - > 2n lines: first n + last n, omitted = len - 2n.
        display_lines excludes the omitted placeholder; the caller renders
        it inline between head and tail.
        """
        all_l = self.all_lines
        n = self._n
        if len(all_l) <= 2 * n:
            return all_l, 0
        return all_l[:n] + all_l[-n:], len(all_l) - 2 * n

    @property
    def merged(self) -> str:
        return "\n".join(self.all_lines)


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
# Prompt text reconstruction (shared by live + replay paths)
# ---------------------------------------------------------------------------


def _prompt_text(blocks: list) -> str:
    """Join a prompt's blocks into the user message text.

    Text blocks concatenate directly. Image blocks are normally rewritten to
    path references before reaching this layer (see prompt_blocks.py); to keep
    legacy/old-log replay robust, a stray raw image block degrades to its
    ``uri`` as text rather than silently disappearing.
    """
    parts: list[str] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "image":
            uri = b.get("uri")
            if uri:
                parts.append(f"Image: {uri}")
    return "".join(parts)


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
            text = _prompt_text(data)
        else:
            text = ""
        self.messages.append({"role": "user", "content": text})

    def _on_session_prompt(self, data: dict) -> None:
        # Legacy format
        self._flush_turn()
        blocks = data.get("prompt", [])
        text = _prompt_text(blocks)
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
            "stream_buf": StreamBuffer(),
        }
        self._tool_order.append(tid)

    def _on_tool_call_update(self, data: dict) -> None:
        tid = data.get("toolCallId", "")
        status = data.get("status", "")

        if tid not in self._pending_tools:
            return

        if not status:
            # Process output (streaming chunk) — accumulate via StreamBuffer
            chunk = data.get("rawOutput", "")
            sep = data.get("separator", "\n")
            if chunk:
                self._pending_tools[tid]["stream_buf"].feed(chunk, sep)
            return

        if status in ("completed", "failed"):
            # Final event: prefer rawOutput, fall back to accumulated stream
            result = _resolve_result(data)
            if not result:
                buf = self._pending_tools[tid].get("stream_buf")
                result = buf.merged if buf else ""
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
            result = tool.get("result", "")

            # Persist large results (live mode only)
            if self._log_stem and len(result) > RESULT_THRESHOLD:
                result = persist_result(self._log_stem, tid, result)

            # Tool result goes to the LLM as-is. For Skill, the return value
            # is already a concise label ("Loaded skill X from <path>"); the
            # full skill content reaches the LLM via a separate user message
            # injected by the user_message_chunk event (no duplication).
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
        self._stream_bufs: dict[str, StreamBuffer] = {}  # tid -> StreamBuffer
        self._rendered = 0  # lines currently rendered in the stream window

    def __call__(self, event_type: str, data: dict) -> None:
        if event_type == "agent_message_chunk":
            self._on_agent_message_chunk(data)
        elif event_type == "agent_thought_chunk":
            self._on_agent_thought_chunk(data)
        elif event_type == "tool_call":
            self._on_tool_call(data)
        elif event_type == "tool_call_update":
            self._on_tool_call_update(data)
        elif event_type == "result":
            self._on_result(data)

    def _on_agent_message_chunk(self, data: dict) -> None:
        # Always write text to stdout — this is the main LLM output
        if "text" in data and "type" in data:
            text = data.get("text", "")
        else:
            text = data.get("content", {}).get("text", "")
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _on_agent_thought_chunk(self, data: dict) -> None:
        # Verbose-only: render reasoning/thinking to stderr (dim) so it
        # stays out of stdout pipes. Non-verbose mode stays quiet.
        if not self._verbose:
            return
        if "text" in data and "type" in data:
            text = data.get("text", "")
        else:
            text = data.get("content", {}).get("text", "")
        if text:
            from .tools._display import _dim
            sys.stderr.write(_dim(f"💭 {text}") + "\n")
            sys.stderr.flush()

    def _on_result(self, data: dict) -> None:
        # Print a token-usage summary to stderr (stays out of stdout pipes).
        usage = data.get("usage")
        if not usage:
            return
        from .tools._display import _dim
        summary = (
            f"📊 tokens: {usage.get('prompt_tokens', 0)} in / "
            f"{usage.get('completion_tokens', 0)} out / "
            f"{usage.get('total_tokens', 0)} total"
        )
        sys.stderr.write(_dim(summary) + "\n")
        sys.stderr.flush()

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
                sep = data.get("separator", "\n")
                buf = self._stream_bufs.setdefault(tid, StreamBuffer())
                buf.feed(raw_output, sep)
                lines, omitted = buf.preview()
                self._rendered = self._render_stream(lines, omitted)
        elif status in ("completed", "failed"):
            if self._verbose:
                # Drain the buffer BEFORE clearing the window. The completed
                # event carries no rawOutput when output was streamed
                # (agent.py emits status only), so reconstruct the full
                # result from the accumulated StreamBuffer here.
                buf = self._stream_bufs.pop(tid, None)
                self._clear_stream()
                success = status == "completed"
                info = self._tool_info.get(tid, {})
                name = info.get("name", "")
                args = info.get("args", {})
                if tid in self._streamed and buf:
                    result = buf.merged
                else:
                    result = raw_output or ""
                print(format_result(name, result, success, args), file=sys.stderr)

    # ------------------------------------------------------------------
    # ANSI streaming window (verbose mode only)
    # ------------------------------------------------------------------

    def _render_stream(self, lines: list[str], omitted: int) -> int:
        """Render the streaming window. Returns the new rendered line count.

        Layout when omitted > 0:  head n + omitted placeholder + tail n.
        Layout when omitted == 0: all lines as-is.
        """
        from .tools._display import use_ansi, _dim, _sym, _BAR, _ASCII_BAR

        bar = _sym(_BAR, _ASCII_BAR)
        n = 5  # head/tail size; matches StreamBuffer default

        # Build the full row list to draw (rows are plain text; _dim applied
        # uniformly so every row has identical escape sequences — required
        # for in-place ANSI redraw to align cleanly).
        rows: list[str] = []
        if omitted > 0:
            head, tail = lines[:n], lines[n:]
            rows = [f"  {bar} {l}" for l in head]
            rows.append(f"  ... ({omitted} lines omitted)")
            rows += [f"  {bar} {l}" for l in tail]
        else:
            rows = [f"  {bar} {l}" for l in lines]

        if not use_ansi():
            # Fallback: without ANSI we can't redraw in place; emit the last
            # row so the user still sees current progress.
            if rows:
                sys.stderr.write(_dim(rows[-1]) + "\n")
                sys.stderr.flush()
            return 0

        new_count = len(rows)
        total = max(self._rendered, new_count)

        # Move cursor up to the top of the rendered region
        if self._rendered > 0:
            sys.stderr.write(f"\033[{self._rendered}A")

        # Clear and redraw each line
        for i in range(total):
            sys.stderr.write("\033[2K")
            if i < new_count:
                sys.stderr.write(_dim(rows[i]))
            sys.stderr.write("\r\n")

        # If we cleared more lines than we drew, move back up
        if total > new_count:
            sys.stderr.write(f"\033[{total - new_count}A")

        sys.stderr.flush()
        return new_count

    def _clear_stream(self) -> None:
        """Clear the rendered streaming window."""
        from .tools._display import use_ansi

        if use_ansi() and self._rendered > 0:
            sys.stderr.write(f"\033[{self._rendered}A\033[J")
            sys.stderr.flush()
        self._rendered = 0
