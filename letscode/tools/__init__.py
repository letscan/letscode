"""Tool definitions and dispatch for letscode."""

from typing import Any

from . import bash, edit, glob, grep, read, skill, write
from . import agent
from ._display import format_call, format_result

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
    return format_call(name, args)


# ---------------------------------------------------------------------------
# Result summary (verbose logging)
# ---------------------------------------------------------------------------


def _result_summary(name: str, result: str, success: bool = True,
                    args: dict[str, Any] | None = None) -> str:
    """Generate a formatted summary of the tool result."""
    return format_result(name, result, success, args or {})


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
