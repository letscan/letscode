"""Shared types for tool modules."""

import os
from collections.abc import Callable
from dataclasses import dataclass

from ..rules import Rules


@dataclass
class ToolResult:
    """Structured result from a tool execution."""
    content: str
    success: bool = True


# Type alias: tool executor function
ToolExecutor = Callable  # (args: dict[str, Any]) -> str | ToolResult

# Security state — set by agent.py at startup
_preset: str = "default"
_sandbox: bool = True
_rules: Rules = Rules()


def set_security(preset: str, sandbox: bool, rules: Rules) -> None:
    global _preset, _sandbox, _rules
    _preset = preset
    _sandbox = sandbox
    _rules = rules


def get_preset() -> str:
    return _preset


def is_sandbox() -> bool:
    return _sandbox


def check_read_allowed(path: str) -> str | None:
    """Check if reading a path is allowed. Returns error msg or None."""
    from ..rules import check_read
    return check_read(path, _rules)


def check_write_allowed(path: str) -> str | None:
    """Check if writing to a path is allowed. Returns error msg or None."""
    from ..rules import check_write
    return check_write(path, _rules)


def check_cmd_allowed(command: str) -> str | None:
    """Check if executing a command is allowed. Returns error msg or None."""
    from ..rules import check_cmd
    return check_cmd(command, _rules)


def get_cwd() -> str:
    return os.getcwd()
