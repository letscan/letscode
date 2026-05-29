"""Shared event log manipulation utilities."""

import json
from pathlib import Path


def read_events(log_path: str) -> list[dict]:
    """Read all events from a JSONL log file."""
    events = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return events


def write_events(log_path: str, events: list[dict]) -> None:
    """Write events to a JSONL log file (overwrites existing)."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def split_turns(events: list[dict]) -> list[list[dict]]:
    """Split events into turns, each starting at a session/prompt event.

    Returns a list of turns, where each turn is a list of events.
    """
    turns: list[list[dict]] = []
    current: list[dict] = []

    for ev in events:
        if ev.get("type") == "session/prompt":
            if current:
                turns.append(current)
            current = [ev]
        else:
            current.append(ev)

    if current:
        turns.append(current)

    return turns


def last_agent_text(turn_events: list[dict]) -> str | None:
    """Extract the last agent message text from a turn's events."""
    chunks: list[str] = []
    for ev in reversed(turn_events):
        if ev.get("type") == "agent_message_chunk":
            text = ev.get("data", {}).get("content", {}).get("text", "")
            if text:
                chunks.append(text)

    if not chunks:
        return None

    full = "".join(reversed(chunks)).strip()
    # Return last non-empty line, truncated
    lines = [l for l in full.splitlines() if l.strip()]
    if not lines:
        return None
    last = lines[-1].strip()
    return last[:120]


def extract_conversation_text(events: list[dict], max_chars: int = 80000) -> str:
    """Extract a readable text transcript from events for LLM summarization.

    Produces lines like:
      User: <prompt text>
      Assistant: <message text>
      [Tool: Bash] $ command...
    """
    parts: list[str] = []

    for ev in events:
        type_ = ev.get("type", "")
        data = ev.get("data", {})

        if type_ == "session/prompt":
            blocks = data.get("prompt", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if text:
                parts.append(f"User: {text}\n")

        elif type_ == "agent_message_chunk":
            text = data.get("content", {}).get("text", "")
            if text:
                parts.append(f"Assistant: {text}\n")

        elif type_ == "tool_call":
            name = data.get("toolName", "")
            inp = data.get("input", {})
            if "command" in inp:
                parts.append(f"[Tool: {name}] $ {inp['command'][:200]}\n")
            elif "file_path" in inp:
                parts.append(f"[Tool: {name}] {inp['file_path']}\n")
            elif "pattern" in inp:
                parts.append(f"[Tool: {name}] {inp['pattern']}\n")

        elif type_ == "tool_call_update":
            status = data.get("status", "")
            if status in ("completed", "failed"):
                summary = data.get("result_summary", "")
                if not summary:
                    result = data.get("result", "")
                    summary = result[:100] if result else ""
                if summary:
                    name = data.get("toolName", "")
                    parts.append(f"[Tool Result: {name}] {summary[:200]}\n")

    text = "".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)"
    return text
