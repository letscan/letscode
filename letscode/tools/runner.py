"""ToolRunner — tool dispatch with security guardrails."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Callable

from ..rules import Rules, check_cmd, check_read, check_write
from ._types import ToolResult

ValidatePath = Callable[[str, str], str | None]
IsFileRead = Callable[[str], bool]


class ToolRunner:
    """Encapsulates tool dispatch, security callbacks, and result handling."""

    def __init__(
        self,
        definitions: list[dict],
        executors: dict[str, Callable],
        *,
        mcp=None,
        rules: Rules | None = None,
        preset: str = "default",
        sandbox: bool = True,
        agent_config: dict | None = None,
    ):
        self._definitions = definitions
        self._executors = executors
        self._mcp = mcp
        self._rules = rules or Rules()
        self._read_files: set[str] = set()
        self._preset = preset
        self._sandbox = sandbox
        self._agent_config = agent_config or {}

    @property
    def definitions(self) -> list[dict]:
        mcp_defs = self._mcp.get_tool_definitions() if self._mcp else []
        return self._definitions + mcp_defs

    @property
    def rules(self) -> Rules:
        return self._rules

    def _make_validate_path(self) -> ValidatePath:
        read_files = self._read_files
        rules = self._rules

        def validate_path(access: str, path: str) -> str | None:
            if access == "read":
                err = check_read(path, rules)
                if err is None:
                    resolved = str(Path(path).expanduser().resolve())
                    read_files.add(resolved)
                return err
            if access == "write":
                return check_write(path, rules)
            return None

        return validate_path

    def _make_is_file_read(self) -> IsFileRead:
        read_files = self._read_files

        def is_file_read(path: str) -> bool:
            resolved = str(Path(path).expanduser().resolve())
            return resolved in read_files

        return is_file_read

    async def execute(
        self, name: str, arguments: str,
    ) -> AsyncGenerator[ToolResult, None]:
        # 1. Parse arguments
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            try:
                args = json.loads(arguments, strict=False) if arguments else {}
            except json.JSONDecodeError as e:
                yield ToolResult(
                    content=f"<error>Invalid JSON arguments for {name}: {e}</error>",
                    success=False,
                )
                return

        # 2. Coarse-grained: command allow/deny
        if name == "Bash":
            command = args.get("command", "")
            if err := check_cmd(command, self._rules):
                yield ToolResult(content=err, success=False)
                return

        # 3. Build security callbacks
        validate_path = self._make_validate_path()
        is_file_read = self._make_is_file_read()

        # 4. Dispatch
        if name.startswith("mcp__") and self._mcp is not None:
            result = await self._mcp.call_tool(name, args)
            yield ToolResult(content=result, success=not result.startswith("<error>"))
            return

        executor = self._executors.get(name)
        if executor is None:
            yield ToolResult(
                content=f"<error>Unknown tool: {name}</error>",
                success=False,
            )
            return

        kwargs: dict[str, Any] = {
            "validate_path": validate_path,
            "is_file_read": is_file_read,
        }
        if name == "Bash":
            kwargs["preset"] = self._preset
            kwargs["sandbox"] = self._sandbox
        elif name == "Agent":
            kwargs.update(self._agent_config)

        result = executor(args, **kwargs)

        if isinstance(result, ToolResult):
            yield result
        else:
            yield ToolResult(content=str(result), success=True)
