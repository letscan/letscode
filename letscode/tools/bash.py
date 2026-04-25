"""Bash tool — shell command execution."""

import os
import subprocess
from typing import Any

from ._types import get_cwd

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
            " - You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). "
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
                    "description": "Optional timeout in milliseconds (max 600000)",
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


def execute(args: dict[str, Any]) -> str:
    command = args.get("command", "")
    timeout_ms = args.get("timeout")
    timeout = (timeout_ms / 1000) if timeout_ms else 120

    shell = os.environ.get("SHELL", "/bin/bash")
    try:
        result = subprocess.run(
            [shell, "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=get_cwd(),
        )
        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout.rstrip("\n"))
        if result.stderr:
            parts.append(result.stderr.rstrip("\n"))
        output = "\n".join(parts) if parts else "(no output)"

        if result.returncode != 0:
            output += f"\n\n[Exit code: {result.returncode}]"
        return output
    except subprocess.TimeoutExpired:
        return f"<error>Command timed out after {timeout}s</error>"
    except Exception as e:
        return f"<error>{e}</error>"
