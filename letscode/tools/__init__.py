"""Tool definitions and dispatch for letscode."""

import json
from typing import Any

from . import bash, edit, glob, grep, read, skill, write

# ---------------------------------------------------------------------------
# Tool schema list (for API calls)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    bash.SCHEMA,
    read.SCHEMA,
    write.SCHEMA,
    edit.SCHEMA,
    glob.SCHEMA,
    grep.SCHEMA,
    skill.SCHEMA,
    # Agent SCHEMA is added dynamically in agent.py to avoid circular import
]

# ---------------------------------------------------------------------------
# Tool executor registry
# ---------------------------------------------------------------------------

_EXECUTORS: dict[str, callable] = {
    "Bash": bash.execute,
    "Read": read.execute,
    "Write": write.execute,
    "Edit": edit.execute,
    "Glob": glob.execute,
    "Grep": grep.execute,
    "Skill": skill.execute,
    # Agent executor is registered dynamically in agent.py
}


# ---------------------------------------------------------------------------
# Verbose call summary
# ---------------------------------------------------------------------------


def _call_summary(name: str, args: dict[str, Any]) -> str:
    """One-line summary of what the tool was called with."""
    if name == "Bash":
        cmd = args.get("command", "")
        first_line = cmd.split("\n")[0][:80]
        desc = args.get("description")
        if desc:
            return f"Bash: {first_line}  ({desc})"
        return f"Bash: {first_line}"

    if name == "Read":
        fp = args.get("file_path", "")
        parts = []
        if args.get("offset"):
            parts.append(f"from line {args['offset']}")
        if args.get("limit"):
            parts.append(f"{args['limit']} lines")
        detail = f" ({', '.join(parts)})" if parts else ""
        return f"Read {fp}{detail}"

    if name == "Write":
        return f"Write {args.get('file_path', '')}"

    if name == "Edit":
        fp = args.get("file_path", "")
        old = args.get("old_string", "").split("\n")[0][:60]
        suffix = " (replace_all)" if args.get("replace_all") else ""
        return f'Edit {fp}: "{old}..."{suffix}'

    if name == "Glob":
        pat = args.get("pattern", "")
        loc = f" in {args['path']}" if args.get("path") else ""
        return f"Glob '{pat}'{loc}"

    if name == "Grep":
        pat = args.get("pattern", "")
        mode = args.get("output_mode", "files_with_matches")
        loc = f" in {args['path']}" if args.get("path") else ""
        return f"Grep '{pat}' ({mode}){loc}"

    if name == "Skill":
        s = args.get("skill", "")
        a = f" {args['args']}" if args.get("args") else ""
        return f"Skill /{s}{a}"

    if name == "Agent":
        desc_text = args.get("description", "")
        sub = args.get("subagent_type", "")
        sub_suffix = f" ({sub})" if sub else ""
        return f"Agent: {desc_text}{sub_suffix}"

    return name


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


def execute_tool(name: str, arguments: str) -> tuple[str, str, bool]:
    """Execute a tool by name. Returns (result_content, call_summary, success)."""
    from ._types import ToolResult

    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        try:
            args = json.loads(arguments, strict=False) if arguments else {}
        except json.JSONDecodeError as e:
            return f"<error>Invalid JSON arguments: {e}</error>", f"{name}: invalid args", False

    executor = _EXECUTORS.get(name)
    if not executor:
        return f"<error>Unknown tool: {name}</error>", f"{name}: unknown", False

    result = executor(args)
    summary = _call_summary(name, args)

    if isinstance(result, ToolResult):
        content = f"<error>{result.content}</error>" if not result.success else result.content
        return content, summary, result.success

    return result, summary, True
