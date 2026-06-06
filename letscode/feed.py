"""Parse JSONL event logs and reconstruct conversation messages."""

import json
from pathlib import Path


def _resolve_result(data: dict) -> str:
    """Get tool result from event data, reading from file if externalized.

    Handles both new format (result field) and legacy format (result_file field).
    """
    if "result" in data:
        return data["result"]
    # Legacy format: result externalized to a file
    result_file = data.get("result_file")
    if result_file:
        try:
            return Path(result_file).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return data.get("result_summary", "")
    return ""


def _extract_text_from_content(content: list) -> str:
    """Safely extract text from a content block list.

    Handles nested structures like [{"type": "content", "content": {"type": "text", "text": "..."}}].
    """
    if not content or not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        # Direct text block
        if item.get("type") == "text":
            return item.get("text", "")
        # Nested content block
        inner = item.get("content")
        if isinstance(inner, dict) and inner.get("type") == "text":
            return inner.get("text", "")
        # Fallback: try text key
        if "text" in item:
            return item["text"]
    return ""


def load_feed(path: str) -> tuple[str, list[dict]]:
    """Load a JSONL event log and rebuild the messages list.

    Returns (original_model, messages) where messages excludes the system prompt.
    The caller should prepend a system prompt before using these messages.
    """
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    if not events:
        return "", []

    # Extract session metadata
    model = ""
    messages: list[dict] = []

    # Accumulation state for the current turn
    text_parts: list[str] = []
    # tool_call_id -> {id, name, arguments, result}
    pending_tools: dict[str, dict] = {}
    tool_order: list[str] = []  # preserve insertion order
    # tool_call_id -> [extra user messages emitted after that tool result]
    extra_after_tool: dict[str, list[dict]] = {}

    def flush_turn():
        """Emit assistant message + tool result messages from accumulated state."""
        nonlocal text_parts, pending_tools, tool_order, extra_after_tool

        if not text_parts and not tool_order:
            return

        # Assistant message
        full_text = "".join(text_parts)
        assistant_msg: dict = {
            "role": "assistant",
            "content": full_text or None,
        }
        if tool_order:
            assistant_msg["tool_calls"] = [
                {
                    "id": pending_tools[tid]["id"],
                    "type": "function",
                    "function": {
                        "name": pending_tools[tid]["name"],
                        "arguments": pending_tools[tid]["arguments"],
                    },
                }
                for tid in tool_order
            ]
        messages.append(assistant_msg)

        # Tool result messages (with extra user messages interleaved)
        for tid in tool_order:
            tool = pending_tools[tid]
            name = tool["name"]
            result = tool.get("result", "")

            # Legacy format: full skill content starts with "[Skill:"
            if name == "Skill" and not result.startswith("<error>") and result.startswith("[Skill:"):
                skill_name = tool.get("input", {}).get("skill", "")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": f"Launching skill: {skill_name}",
                })
                messages.append({
                    "role": "user",
                    "content": result,
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": result,
                })

            # Extra user messages associated with this tool (e.g., skill expansion)
            for msg in extra_after_tool.get(tid, []):
                messages.append(msg)

        text_parts = []
        pending_tools = {}
        tool_order = []
        extra_after_tool = {}

    for ev in events:
        type_ = ev.get("type", "")
        data = ev.get("data", {})

        if type_ == "session/prompt":
            model = data.get("model", "")
            prompt_blocks = data.get("prompt", [])
            prompt_text = "".join(
                b.get("text", "") for b in prompt_blocks if b.get("type") == "text"
            )
            messages.append({"role": "user", "content": prompt_text})

        elif type_ == "agent_message_chunk":
            # New text after tool_calls means a new turn boundary
            if tool_order:
                flush_turn()
            text_parts.append(data.get("content", {}).get("text", ""))

        elif type_ == "tool_call":
            tid = data.get("toolCallId", "")
            inp = data.get("input", {})
            name = data.get("toolName") or _infer_tool_name(inp)
            pending_tools[tid] = {
                "id": tid,
                "name": name,
                "arguments": json.dumps(inp, ensure_ascii=False),
                "input": inp,
            }
            tool_order.append(tid)

        elif type_ == "tool_call_update":
            tid = data.get("toolCallId", "")
            status = data.get("status", "")
            if status == "completed" and tid in pending_tools:
                pending_tools[tid]["result"] = _resolve_result(data)
            elif status == "failed" and tid in pending_tools:
                result_text = _resolve_result(data)
                if not result_text:
                    result_text = _extract_text_from_content(data.get("content", []))
                if not result_text:
                    result_text = "failed"
                pending_tools[tid]["result"] = f"<error>{result_text}</error>"

        elif type_ == "user_message":
            text = data.get("content", {}).get("text", "")
            if text and tool_order:
                last_tid = tool_order[-1]
                extra_after_tool.setdefault(last_tid, []).append(
                    {"role": "user", "content": text}
                )

    # Flush any remaining turn
    flush_turn()

    return model, messages


def _infer_tool_name(inp: dict) -> str:
    """Infer tool name from input argument keys."""
    keys = set(inp.keys())
    if "command" in keys:
        return "Bash"
    if "file_path" in keys and ("old_string" in keys or "new_string" in keys):
        return "Edit"
    if "file_path" in keys and "content" in keys:
        return "Write"
    if "file_path" in keys:
        return "Read"
    if "pattern" in keys and "glob" in keys:
        return "Glob"
    if "pattern" in keys:
        return "Grep"
    if "skill" in keys:
        return "Skill"
    if "prompt" in keys:
        return "Agent"
    return "Unknown"
