"""Event stream emitter — writes JSONL events to log file and optionally stdout."""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

# Tool results larger than this are persisted to disk
RESULT_THRESHOLD = 32 * 1024  # 32KB


# ---------------------------------------------------------------------------
# Global emitter singleton
# ---------------------------------------------------------------------------

_emitter: "EventEmitter | None" = None


def set_emitter(e: "EventEmitter | None") -> None:
    """Register the global event emitter for this session."""
    global _emitter
    _emitter = e


def get_emitter() -> "EventEmitter | None":
    """Return the current session's event emitter, if any."""
    return _emitter


def _now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class EventEmitter:
    """Emits JSONL events to a log file and optionally to stdout."""

    def __init__(self, log_dir: Path, to_stdout: bool = False,
                 append_path: str | None = None):
        self.to_stdout = to_stdout
        self._start_time: float | None = None
        self._turns = 0
        self._tool_calls = 0
        self._log_path: Path | None = None
        self._log_file: Any = None

        if append_path:
            self._log_path = Path(append_path)
            self._log_file = open(self._log_path, "a", encoding="utf-8")
        else:
            log_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            short_id = uuid.uuid4().hex[:4]
            filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{short_id}.jsonl"
            self._log_path = log_dir / filename
            self._log_file = open(self._log_path, "w", encoding="utf-8")

    def set_turns(self, turns: int) -> None:
        self._turns = turns

    def emit(self, type_: str, data: dict) -> None:
        event = {
            "type": type_,
            "timestamp": _now(),
            "data": data,
        }
        line = json.dumps(event, ensure_ascii=False)
        self._log_file.write(line + "\n")
        self._log_file.flush()
        if self.to_stdout:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def emit_init(self, *, model: str, cwd: str, max_tokens: int,
                  max_turns: int, preset: str, sandbox: bool,
                  tools: list[str], mcp_servers: dict | None = None,
                  skills: list[str] | None = None,
                  rules: dict | None = None) -> None:
        import time
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

    def emit_prompt(self, prompt: str,
                    prompt_blocks: list[dict] | None = None) -> None:
        self.emit("prompt", prompt_blocks if prompt_blocks else [{"type": "text", "text": prompt}])

    def emit_agent_message_chunk(self, text: str) -> None:
        self.emit("agent_message_chunk", {
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

    def _write_result_file(self, tool_call_id: str, result: str) -> Path:
        """Write a large result to a separate file. Returns the file path."""
        results_dir = self._log_path.parent / (self._log_path.stem + "_results")
        results_dir.mkdir(parents=True, exist_ok=True)
        result_path = results_dir / f"{tool_call_id}.txt"
        result_path.write_text(result, encoding="utf-8")
        return result_path

    def emit_tool_update(self, tool_call_id: str, status: str,
                         raw_output: str | None = None) -> None:
        data: dict = {
            "toolCallId": tool_call_id,
            "status": status,
        }
        if raw_output is not None:
            data["rawOutput"] = raw_output
        self.emit("tool_call_update", data)

    def emit_user_message_chunk(self, content: str) -> None:
        """Emit a synthetic user message event (e.g., expanded skill prompt).

        Only written to log for feed reconstruction. Not translated to ACP.
        """
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
        import time
        data: dict = {
            "stopReason": stop_reason,
            "turns": self._turns,
            "toolCalls": self._tool_calls,
        }
        if self._start_time is not None:
            data["duration_ms"] = int((time.monotonic() - self._start_time) * 1000)
        self.emit("result", data)
        self.close()

    def close(self) -> None:
        if self._log_file and not self._log_file.closed:
            self._log_file.close()

    # ------------------------------------------------------------------
    # High-level convenience methods
    # ------------------------------------------------------------------

    def on_text_line(self, text: str) -> None:
        """Emit agent_message_chunk + write plain text to stdout if not event-stream mode."""
        self.emit_agent_message_chunk(text)
        if not self.to_stdout:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def persist_result(self, result: str, tool_id: str) -> str:
        """Persist a large tool result to disk. Returns the reference message."""
        result_path = self._write_result_file(tool_id, result)

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

    def on_session_end(self, stop_reason: str) -> None:
        """Emit the session result event (end_turn)."""
        self.emit_result(stop_reason)
