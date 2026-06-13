"""Tool definitions and dispatch for letscode."""

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
