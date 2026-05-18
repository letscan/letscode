"""Glob tool — file pattern matching via rg or pathlib fallback."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._types import get_cwd, check_read_allowed

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
        if err := check_read_allowed(search_path):
            return err

        base = Path(search_path).expanduser().resolve()
        if not base.is_dir():
            return f"<error>{search_path} is not a directory</error>"

        if shutil.which("rg") is not None:
            files = _search_rg(pattern, base)
        else:
            files = _search_pathlib(pattern, base)

        truncated = len(files) > MAX_RESULTS
        if truncated:
            files = files[:MAX_RESULTS]

        lines = []
        for f in files:
            if check_read_allowed(str(f)) is not None:
                continue
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


def _search_rg(pattern: str, base: Path) -> list[Path]:
    """Use ripgrep --files for fast file listing, then filter by glob pattern."""
    # rg --files lists all files; we use --glob to filter
    cmd = ["rg", "--files", "--color=never"]
    # Pass the glob pattern to rg
    cmd.extend(["--glob", pattern])
    cmd.append(str(base))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and not result.stdout:
            return []
        files = []
        for line in result.stdout.strip().split("\n"):
            if line:
                p = Path(line)
                if p.is_file():
                    files.append(p)
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return files
    except (subprocess.TimeoutExpired, Exception):
        return _search_pathlib(pattern, base)


def _search_pathlib(pattern: str, base: Path) -> list[Path]:
    """Fallback using pathlib.glob."""
    matches = list(base.glob(pattern))
    files = [m for m in matches if m.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files
