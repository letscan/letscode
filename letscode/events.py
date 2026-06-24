"""EventHub — internal event bus with pluggable subscribers."""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .subscribers import RESULT_THRESHOLD, StreamBuffer
from .tools._display import format_call, format_result


# ---------------------------------------------------------------------------
# Shared persistence helpers
# ---------------------------------------------------------------------------

def _write_result_file(log_path: Path, tool_call_id: str, result: str) -> Path:
    results_dir = log_path.parent / (log_path.stem + "_results")
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{tool_call_id}.txt"
    result_path.write_text(result, encoding="utf-8")
    return result_path


def _make_persisted_ref(result: str, result_path: Path) -> str:
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
# Timestamp helper
# ---------------------------------------------------------------------------

def _now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Global hub singleton
# ---------------------------------------------------------------------------

_hub: "EventHub | None" = None


def set_hub(h: "EventHub | None") -> None:
    global _hub
    _hub = h


def get_hub() -> "EventHub | None":
    return _hub


# ---------------------------------------------------------------------------
# EventHub
# ---------------------------------------------------------------------------

class EventHub:
    """Internal event bus. All producers emit events; subscribers consume them."""

    def __init__(self):
        self._subscribers: list[Callable[[str, dict], None]] = []
        self._start_time: float | None = None
        self._turns = 0
        self._tool_calls = 0

    def subscribe(self, handler: Callable[[str, dict], None]) -> None:
        self._subscribers.append(handler)

    def emit(self, event_type: str, data: dict) -> None:
        for handler in self._subscribers:
            handler(event_type, data)

    def close(self) -> None:
        for handler in self._subscribers:
            close_fn = getattr(handler, "close", None)
            if close_fn:
                close_fn()

    def set_turns(self, turns: int) -> None:
        self._turns = turns

    # ------------------------------------------------------------------
    # Convenience methods (high-level API used by agent.py)
    # ------------------------------------------------------------------

    def emit_init(self, *, model: str, cwd: str, max_tokens: int,
                  max_turns: int, preset: str, sandbox: bool,
                  tools: list[str], mcp_servers: dict | None = None,
                  skills: list[str] | None = None,
                  rules: dict | None = None) -> None:
        self._start_time = time.monotonic()
        data: dict = {
            "agent": "letscode",
            "version": __version__,
            "model": model,
            "cwd": cwd,
            "maxTokens": max_tokens,
            "maxTurns": max_turns,
            "preset": preset,
            "sandbox": sandbox,
            "tools": tools,
        }
        if mcp_servers:
            data["mcpServers"] = mcp_servers
        if skills:
            data["skills"] = skills
        if rules:
            data["rules"] = rules
        self.emit("init", data)

    def emit_prompt(self, prompt_blocks: list[dict] | None = None,
                    prompt: str = "") -> None:
        self.emit("prompt", prompt_blocks if prompt_blocks else [{"type": "text", "text": prompt}])

    def emit_agent_message_chunk(self, text: str) -> None:
        self.emit("agent_message_chunk", {
            "type": "text",
            "text": text,
        })

    def emit_agent_thought_chunk(self, text: str) -> None:
        """Emit a reasoning/thinking chunk (e.g. GLM reasoning_content).

        Symmetric to emit_agent_message_chunk. Display-only: thoughts are
        NOT fed back into the LLM history (MessageSubscriber ignores them).
        """
        self.emit("agent_thought_chunk", {
            "type": "text",
            "text": text,
        })

    def emit_tool_call(self, tool_call_id: str, name: str, args: dict) -> None:
        self._tool_calls += 1
        self.emit("tool_call", {
            "toolCallId": tool_call_id,
            "toolName": name,
            "rawInput": args,
        })

    def emit_tool_update(self, tool_call_id: str, status: str | None = None,
                         raw_output: str | None = None,
                         separator: str | None = None) -> None:
        data: dict = {"toolCallId": tool_call_id}
        if status is not None:
            data["status"] = status
        if raw_output is not None:
            data["rawOutput"] = raw_output
        if separator is not None:
            data["separator"] = separator
        self.emit("tool_call_update", data)

    def emit_user_message_chunk(self, content: str) -> None:
        self.emit("user_message_chunk", {
            "type": "text",
            "text": content,
        })

    def emit_error(self, message: str, code: str = "unknown",
                   recoverable: bool = False) -> None:
        self.emit("error", {
            "message": message,
            "code": code,
            "recoverable": recoverable,
        })

    def emit_result(self, stop_reason: str) -> None:
        data: dict = {
            "stopReason": stop_reason,
            "turns": self._turns,
            "toolCalls": self._tool_calls,
        }
        if self._start_time is not None:
            data["duration_ms"] = int((time.monotonic() - self._start_time) * 1000)
        self.emit("result", data)
        self.close()

    def on_text_line(self, text: str) -> None:
        """Emit agent_message_chunk event."""
        self.emit_agent_message_chunk(text)

    def on_thought_line(self, text: str) -> None:
        """Emit agent_thought_chunk event."""
        self.emit_agent_thought_chunk(text)

    def on_session_end(self, stop_reason: str) -> None:
        self.emit_result(stop_reason)


# ---------------------------------------------------------------------------
# LogSubscriber — human-readable debug log (always registered, NOT a feed)
# ---------------------------------------------------------------------------

class LogSubscriber:
    """Writes a human-readable debug log to .letscode/logs/*.log.

    Always registered. Intentionally NOT jsonl: this is an internal debug
    log, not a replay feed. Using a non-JSON format structurally prevents
    it from being mistaken for a feed file (read_events json.loads would
    fail on it). The replay/continuation feed is a separate concern owned
    by FeedOutputSubscriber (--output). Large tool outputs are summarized
    (size + line count) rather than written in full to keep the log small.
    """

    # Truncate any single summary field beyond this many chars
    _SUMMARY_LIMIT = 200

    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        short_id = uuid.uuid4().hex[:4]
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{short_id}.log"
        self._log_path = log_dir / filename
        self._log_file = open(self._log_path, "w", encoding="utf-8")

    @property
    def log_path(self) -> Path:
        return self._log_path

    def __call__(self, event_type: str, data: dict) -> None:
        summary = self._summarize(event_type, data)
        self._write(_now(), "INFO", event_type, summary)

    def log_debug(self, message: str) -> None:
        """Write a debug line that is not emitted as an event."""
        self._write(_now(), "DEBUG", "debug", message)

    def close(self) -> None:
        if self._log_file and not self._log_file.closed:
            self._log_file.close()

    # -- internals --

    def _write(self, ts: str, level: str, event_type: str, summary: str) -> None:
        # Truncate over-long single-line summaries
        if len(summary) > self._SUMMARY_LIMIT:
            summary = summary[: self._SUMMARY_LIMIT] + "…"
        line = f"[{ts}] {level} {event_type}: {summary}\n"
        self._log_file.write(line)
        self._log_file.flush()

    def _summarize(self, event_type: str, data: dict) -> str:
        if event_type == "prompt":
            return self._prompt_summary(data)
        if event_type == "agent_message_chunk":
            return data.get("text", "")
        if event_type == "agent_thought_chunk":
            # Prefix so the debug log distinguishes thinking from the response
            return f"💭 {data.get('text', '')}"
        if event_type == "tool_call":
            return self._tool_call_summary(data)
        if event_type == "tool_call_update":
            return self._tool_update_summary(data)
        if event_type == "user_message_chunk":
            return data.get("text", "")
        if event_type == "error":
            return data.get("message", "")
        if event_type == "result":
            return data.get("text", "")
        if event_type == "init":
            return data.get("model", "")
        # Fallback: compact JSON of the data
        return json.dumps(data, ensure_ascii=False)

    def _prompt_summary(self, data) -> str:
        if isinstance(data, dict):
            # prompt_blocks form
            return " ".join(
                b.get("text", "") for b in data if isinstance(b, dict) and b.get("type") == "text"
            )
        if isinstance(data, list):
            return " ".join(
                b.get("text", "") for b in data if isinstance(b, dict) and b.get("type") == "text"
            )
        return str(data)

    def _tool_call_summary(self, data: dict) -> str:
        name = data.get("toolName", "?")
        args = data.get("rawInput", {})
        # Compact one-liner of the args
        arg_str = json.dumps(args, ensure_ascii=False)
        return f"{name} ({arg_str})"

    def _tool_update_summary(self, data: dict) -> str:
        status = data.get("status")
        raw = data.get("rawOutput")
        sep = data.get("separator")
        if status in ("completed", "failed"):
            if raw:
                n = raw.count("\n") + 1
                size = len(raw)
                return f"{status} ({n} lines, {size} bytes)"
            return f"{status}"
        if raw is not None:
            # Streaming chunk
            return f"chunk ({sep!r}): {raw}"
        return f"{status or 'update'}"


# ---------------------------------------------------------------------------
# FeedOutputSubscriber — consolidated output for replay/sharing (--output)
# ---------------------------------------------------------------------------

class FeedOutputSubscriber:
    """Writes consolidated agent output to a file (--output flag).

    Mode determines format:
      - "json": structured JSONL feed (process output merged into final
                tool_call_update; large results persisted). Compatible with
                --feed replay.
      - "verbose": human-readable text + consolidated tool call/result lines.
      - "text": human-readable text only (LLM responses).

    In all modes, per-line process-output events are accumulated in memory
    and merged into the final completed/failed event, so the file never
    contains transient streaming chunks.
    """

    def __init__(self, path: str, mode: str):
        self._mode = mode
        self._stream_bufs: dict[str, StreamBuffer] = {}
        self._tool_info: dict[str, dict] = {}  # tid -> {name, args}
        self._log_path = Path(path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Always append: lets --output be reused across multi-turn runs
        self._file = open(self._log_path, "a", encoding="utf-8")

    def __call__(self, event_type: str, data: dict) -> None:
        # Accumulate process-output chunks; merge on final event
        if event_type == "tool_call_update":
            tid = data.get("toolCallId", "")
            status = data.get("status")
            if not status:
                chunk = data.get("rawOutput", "")
                sep = data.get("separator", "\n")
                if chunk:
                    self._stream_bufs.setdefault(tid, StreamBuffer()).feed(chunk, sep)
                return
            buf = self._stream_bufs.pop(tid, None)
            if buf and buf.all_lines and "rawOutput" not in data:
                data = dict(data)
                data["rawOutput"] = buf.merged
            if self._mode == "json":
                data = self._maybe_persist(data)

        if self._mode == "json":
            self._write_json(event_type, data)
        elif self._mode == "verbose":
            self._write_verbose(event_type, data)
        else:
            self._write_text(event_type, data)

    # -- mode writers --

    def _write_json(self, event_type: str, data: dict) -> None:
        event = {"type": event_type, "timestamp": _now(), "data": data}
        self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._file.flush()

    def _write_text(self, event_type: str, data: dict) -> None:
        if event_type != "agent_message_chunk":
            return
        text = data.get("text", "") if "text" in data else data.get("content", {}).get("text", "")
        if text:
            self._file.write(text + "\n")
            self._file.flush()

    def _write_verbose(self, event_type: str, data: dict) -> None:
        if event_type == "agent_message_chunk":
            text = data.get("text", "") if "text" in data else data.get("content", {}).get("text", "")
            if text:
                self._file.write(text + "\n")
                self._file.flush()
        elif event_type == "agent_thought_chunk":
            text = data.get("text", "") if "text" in data else data.get("content", {}).get("text", "")
            if text:
                self._file.write(f"💭 {text}\n")
                self._file.flush()
        elif event_type == "tool_call":
            tid = data.get("toolCallId", "")
            name = data.get("toolName", "")
            args = data.get("rawInput", {})
            self._tool_info[tid] = {"name": name, "args": args}
            self._file.write(format_call(name, args) + "\n")
        elif event_type == "tool_call_update":
            status = data.get("status")
            if status in ("completed", "failed"):
                tid = data.get("toolCallId", "")
                info = self._tool_info.get(tid, {})
                name = info.get("name", "")
                args = info.get("args", {})
                result = data.get("rawOutput", "")
                success = status == "completed"
                self._file.write(format_result(name, result, success, args) + "\n")
                self._file.flush()

    # -- helpers --

    def _maybe_persist(self, data: dict) -> dict:
        raw = data.get("rawOutput")
        if raw and len(raw) > RESULT_THRESHOLD:
            result_path = _write_result_file(self._log_path, data["toolCallId"], raw)
            data = dict(data)
            data["rawOutput"] = _make_persisted_ref(raw, result_path)
        return data

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.close()


# ---------------------------------------------------------------------------
# StreamSubscriber — real-time JSONL to stdout (--event-stream mode)
# ---------------------------------------------------------------------------

class StreamSubscriber:
    """Writes JSONL events to stdout (--event-stream mode).

    Emits every event in real time, including per-line process output,
    for live consumers (e.g. ACP server).
    """

    def __call__(self, event_type: str, data: dict) -> None:
        event = {
            "type": event_type,
            "timestamp": _now(),
            "data": data,
        }
        line = json.dumps(event, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
