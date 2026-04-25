"""Glob tool — file pattern matching."""

from pathlib import Path
from typing import Any

from ._types import get_cwd

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": (
            "- Fast file pattern matching tool that works with any codebase size\n"
            "- Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\"\n"
            "- Returns matching file paths sorted by modification time\n"
            "- Use this tool when you need to find files by name patterns\n"
            "- When you are doing an open ended search that may require multiple rounds "
            "of globbing and grepping, consider using a more targeted approach first"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. If not specified, the current "
                    "working directory will be used. IMPORTANT: Omit this field to use the "
                    "default directory. Must be a valid directory path if provided.",
                },
            },
            "required": ["pattern"],
        },
    },
}

MAX_RESULTS = 1000


def execute(args: dict[str, Any]) -> str:
    pattern = args.get("pattern", "")
    search_path = args.get("path") or get_cwd()

    try:
        base = Path(search_path).expanduser().resolve()
        if not base.is_dir():
            return f"<error>{search_path} is not a directory</error>"

        matches = list(base.glob(pattern))
        files = [m for m in matches if m.is_file()]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        truncated = len(files) > MAX_RESULTS
        if truncated:
            files = files[:MAX_RESULTS]

        lines = []
        for f in files:
            try:
                lines.append(str(f.relative_to(base)))
            except ValueError:
                lines.append(str(f))

        if not lines:
            return "No files found"

        result = "\n".join(lines)
        if truncated:
            result += "\n\n(Results are truncated. Consider using a more specific path or pattern.)"
        return result
    except Exception as e:
        return f"<error>{e}</error>"
