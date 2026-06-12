"""Tool definitions and dispatch for letscode."""

from typing import Any

from . import bash, edit, glob, grep, read, skill, write
from . import agent

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
    agent.SCHEMA,
]


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
# Result summary (verbose logging)
# ---------------------------------------------------------------------------


def _result_summary(name: str, result: str) -> str:
    """Generate a one-line summary of the tool result."""
    if result.startswith("<error>"):
        msg = result.removeprefix("<error>").removesuffix("</error>").strip()
        return f"ERROR: {msg}"
    if name == "Bash":
        lines = result.strip().split("\n")
        last = lines[-1].strip() if lines else ""
        if len(lines) > 1:
            return f"{len(lines)} lines"
        return last[:80] if last else "(no output)"
    if name == "Read":
        return f"{len(result.strip().splitlines())} lines"
    if name == "Write":
        return result
    if name == "Edit":
        return result
    if name == "Glob":
        lines = result.strip().split("\n")
        if "truncated" in result:
            return f"{len(lines)} files (truncated)"
        return f"{len(lines)} files"
    if name == "Grep":
        if result.startswith("Found "):
            return result.split("\n")[0]
        if result.startswith("No matches"):
            return "No matches"
        return f"{len(result.strip().splitlines())} lines"
    if name == "Skill":
        if result.startswith("<error>"):
            msg = result.removeprefix("<error>").removesuffix("</error>").strip()
            return f"ERROR: {msg}"
        return f"{len(result.strip().splitlines())} lines"
    if name == "Agent":
        return result.split("\n")[0][:100]
    if name.startswith("mcp__"):
        if result.startswith("<error>"):
            msg = result.removeprefix("<error>").removesuffix("</error>").strip()
            return f"ERROR: {msg}"
        return result.split("\n")[0][:100]
    return "ok"


# ---------------------------------------------------------------------------
# Executor registry (for ToolRunner)
# ---------------------------------------------------------------------------

EXECUTORS: dict[str, callable] = {
    "Bash": bash.execute,
    "Read": read.execute,
    "Write": write.execute,
    "Edit": edit.execute,
    "Glob": glob.execute,
    "Grep": grep.execute,
    "Skill": skill.execute,
    "Agent": agent.execute,
}
