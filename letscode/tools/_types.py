"""Shared types for tool modules."""

import os
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Structured result from a tool execution."""
    content: str
    success: bool = True


def get_cwd() -> str:
    return os.getcwd()
