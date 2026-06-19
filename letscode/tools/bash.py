"""Bash tool — shell command execution with streaming output."""

import asyncio
import os
import pty
import shutil
import time
from collections.abc import AsyncGenerator
from typing import Any

from ._types import ToolResult
from .runner import ToolOutput


async def _read_records(stream: asyncio.StreamReader) -> AsyncGenerator[tuple[str, str], None]:
    """Read from a stream, yielding (record, separator) split on \\r or \\n.

    separator is "\\r" or "\\n" indicating how the record was terminated.
    \\r\\n is treated as a single "\\n" separator.
    """
    buf = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            if buf:
                text = buf.decode(errors="replace").strip()
                if text:
                    yield text, "\n"
            return
        buf += chunk
        while True:
            cr = buf.find(b"\r")
            nl = buf.find(b"\n")
            if cr < 0 and nl < 0:
                break
            if cr < 0:
                pos, sep = nl, "\n"
            elif nl < 0:
                pos, sep = cr, "\r"
            elif cr < nl:
                pos, sep = cr, "\r"
            else:
                pos, sep = nl, "\n"
            record = buf[:pos].decode(errors="replace")
            buf = buf[pos + 1:]
            if sep == "\r" and buf.startswith(b"\n"):
                buf = buf[1:]
                sep = "\n"
            if record:
                yield record, sep

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": (
            "Executes a given bash command and returns its output.\n\n"
            "The working directory persists between commands, but shell state does not. "
            "The shell environment is initialized from the user's profile (bash or zsh).\n\n"
            "IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head`, "
            "`tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed or "
            "after you have verified that a dedicated tool cannot accomplish your task. "
            "Instead, use the appropriate dedicated tool as this will provide a much "
            "better experience for the user:\n"
            " - File search: Use Glob (NOT find or ls)\n"
            " - Content search: Use Grep (NOT grep or rg)\n"
            " - Read files: Use Read (NOT cat/head/tail)\n"
            " - Edit files: Use Edit (NOT sed/awk)\n"
            " - Write files: Use Write (NOT echo >/cat <<EOF)\n"
            " - Communication: Output text directly (NOT echo/printf)\n"
            "While the Bash tool can do similar things, it's better to use the built-in "
            "tools as they provide a better user experience and make it easier to review "
            "tool calls and give permission.\n\n"
            "# Instructions\n"
            " - If your command will create new directories or files, first use this tool "
            "to run `ls` to verify the parent directory exists and is the correct location.\n"
            " - Always quote file paths that contain spaces with double quotes in your command "
            '(e.g., cd "path with spaces/file.txt")\n'
            " - Try to maintain your current working directory throughout the session by using "
            "absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly "
            "requests it.\n"
            " - You may specify an optional timeout in milliseconds (up to 1800000ms / 30 minutes). "
            "By default, your command will timeout after 120000ms (2 minutes).\n"
            " - When issuing multiple commands:\n"
            "  - If the commands are independent and can run in parallel, make multiple Bash "
            "tool calls in a single message.\n"
            "  - If the commands depend on each other and must run sequentially, use a single "
            "Bash call with '&&' to chain them together.\n"
            "  - Use ';' only when you need to run commands sequentially but don't care if "
            "earlier commands fail.\n"
            "  - DO NOT use newlines to separate commands (newlines are ok in quoted strings).\n"
            " - For git commands:\n"
            "  - Prefer to create a new commit rather than amending an existing commit.\n"
            "  - Before running destructive operations (e.g., git reset --hard, git push --force, "
            "git checkout --), consider whether there is a safer alternative.\n"
            "  - Never skip hooks (--no-verify) or bypass signing (--no-gpg-sign) unless the "
            "user has explicitly asked for it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout in milliseconds (max 1800000)",
                },
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does",
                },
            },
            "required": ["command"],
        },
    },
}


async def execute(
    args: dict[str, Any], *, preset: str = "default", sandbox: bool = True, **_,
) -> AsyncGenerator[ToolOutput | ToolResult, None]:
    command = args.get("command", "")
    timeout_ms = args.get("timeout")
    timeout = min(timeout_ms / 1000, 1800) if timeout_ms else 120

    shell = os.environ.get("SHELL", "/bin/bash")
    cwd = os.getcwd()
    cmd = [shell, "-c", command]

    if sandbox and preset in ("safe", "default", "risk") and shutil.which("sandbox-exec"):
        from ..sandbox import wrap_command
        cmd = wrap_command(cmd, cwd, preset)

    try:
        # PTY forces line-buffered output from the subprocess,
        # so progress bars and interactive output stream in real time.
        master_fd, slave_fd = pty.openpty()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=slave_fd,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        os.close(slave_fd)

        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            os.fdopen(master_fd, "rb"),
        )

        lines: list[str] = []
        start = time.monotonic()

        async for record, sep in _read_records(reader):
            if time.monotonic() - start > timeout:
                proc.kill()
                transport.close()
                yield ToolResult(content=f"Command timed out after {timeout}s", success=False)
                return
            lines.append(record)
            yield ToolOutput(content=record, separator=sep)

        await proc.wait()
        transport.close()
        stderr_data = await proc.stderr.read()
        stderr_text = stderr_data.decode(errors="replace").rstrip("\n") if stderr_data else ""

        parts = list(lines)
        if stderr_text:
            parts.append(stderr_text)
        output = "\n".join(parts) if parts else "(no output)"

        success = proc.returncode == 0
        if not success:
            output += f"\n\n[Exit code: {proc.returncode}]"

        yield ToolResult(content=output, success=success)

    except Exception as e:
        yield ToolResult(content=str(e), success=False)
