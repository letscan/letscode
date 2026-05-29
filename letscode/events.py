"""Event stream emitter — writes JSONL events to log file and optionally stdout."""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

# Results larger than this are written to separate files
_RESULT_FILE_THRESHOLD = 32 * 1024  # 32KB


# ACP tool kind mapping
_TOOL_KINDS: dict[str, str] = {
    "Read": "read",
    "Write": "edit",
    "Edit": "edit",
    "Bash": "other",
    "Glob": "search",
    "Grep": "search",
    "Skill": "other",
    "Agent": "other",
}


def _tool_title(name: str, args: dict) -> str:
    if name == "Read":
        return f"Reading {args.get('file_path', '')}"
    if name == "Write":
        return f"Writing {args.get('file_path', '')}"
    if name == "Edit":
        return f"Editing {args.get('file_path', '')}"
    if name == "Bash":
        cmd = args.get("command", "")
        return "$ " + cmd.split("\n")[0][:80]
    if name == "Glob":
        return f"Searching files: {args.get('pattern', '')}"
    if name == "Grep":
        return f"Searching: {args.get('pattern', '')}"
    if name == "Skill":
        return f"Running skill: {args.get('skill', '')}"
    if name == "Agent":
        return f"Sub-agent: {args.get('prompt', '')[:50]}"
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        return parts[1] if len(parts) == 2 else name
    return name


def _tool_kind(name: str) -> str:
    if name.startswith("mcp__"):
        return "other"
    return _TOOL_KINDS.get(name, "other")


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

    def emit_session_prompt(self, model: str, cwd: str, prompt: str,
                            prompt_blocks: list[dict] | None = None) -> None:
        import time
        self._start_time = time.monotonic()
        self.emit("session/prompt", {
            "agent": "letscode",
            "version": __version__,
            "model": model,
            "cwd": cwd,
            "prompt": prompt_blocks if prompt_blocks else [{"type": "text", "text": prompt}],
        })

    def emit_agent_message_chunk(self, text: str) -> None:
        self.emit("agent_message_chunk", {
            "content": {"type": "text", "text": text},
        })

    def emit_tool_call(self, tool_call_id: str, name: str, args: dict) -> None:
        self._tool_calls += 1
        self.emit("tool_call", {
            "toolCallId": tool_call_id,
            "toolName": name,
            "title": _tool_title(name, args),
            "kind": _tool_kind(name),
            "status": "pending",
            "input": args,
        })

    def _write_result_file(self, tool_call_id: str, result: str) -> Path:
        """Write a large result to a separate file. Returns the file path."""
        results_dir = self._log_path.parent / (self._log_path.stem + "_results")
        results_dir.mkdir(parents=True, exist_ok=True)
        result_path = results_dir / f"{tool_call_id}.txt"
        result_path.write_text(result, encoding="utf-8")
        return result_path

    def emit_tool_update(self, tool_call_id: str, status: str,
                         content_text: str | None = None,
                         result: str | None = None,
                         duration_ms: int | None = None,
                         tool_name: str | None = None) -> None:
        data: dict = {
            "toolCallId": tool_call_id,
            "status": status,
        }
        if tool_name is not None:
            data["toolName"] = tool_name
        if content_text is not None:
            data["content"] = [
                {"type": "content", "content": {"type": "text", "text": content_text}},
            ]
        if result is not None:
            if len(result) > _RESULT_FILE_THRESHOLD:
                result_path = self._write_result_file(tool_call_id, result)
                data["result_file"] = str(result_path)
                data["result_summary"] = content_text or result[:200]
            else:
                data["result"] = result
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        self.emit("tool_call_update", data)

    def emit_error(self, message: str, code: str = "unknown",
                   recoverable: bool = False) -> None:
        self.emit("error", {
            "message": message,
            "code": code,
            "recoverable": recoverable,
        })

    def emit_session_result(self, stop_reason: str) -> None:
        import time
        data: dict = {
            "stopReason": stop_reason,
            "turns": self._turns,
            "toolCalls": self._tool_calls,
        }
        if self._start_time is not None:
            data["duration_ms"] = int((time.monotonic() - self._start_time) * 1000)
        self.emit("session/result", data)
        self.close()

    def close(self) -> None:
        if self._log_file and not self._log_file.closed:
            self._log_file.close()
