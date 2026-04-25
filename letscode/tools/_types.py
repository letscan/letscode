"""Shared types for tool modules."""

import os
from typing import Any

# Type alias: tool executor function
ToolExecutor = callable  # (args: dict[str, Any]) -> str


def get_cwd() -> str:
    return os.getcwd()
