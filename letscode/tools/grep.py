"""Grep tool — content search with regex, backed by rg or system grep."""

import shutil
import subprocess
from typing import Any

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": (
            "A powerful search tool built on ripgrep\n\n"
            "Usage:\n"
            "- ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command. "
            "The Grep tool has been optimized for correct permissions and access.\n"
            "- Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n"
            "- Filter files with glob parameter (e.g., \"*.js\", \"**/*.tsx\") or type parameter "
            "(e.g., \"js\", \"py\", \"rust\")\n"
            "- Output modes: \"content\" shows matching lines, \"files_with_matches\" shows only "
            "file paths (default), \"count\" shows match counts\n"
            "- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping "
            "(use `interface\\{\\}` to find `interface{}` in Go code)\n"
            "- Multiline matching: By default patterns match within single lines only. For "
            "cross-line patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regular expression pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to current working directory.",
                },
                "glob": {
                    "type": "string",
                    "description": 'Glob pattern to filter files (e.g. "*.js", "**/*.tsx") - maps to rg --glob',
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        'Output mode: "content" shows matching lines (supports -A/-B/C context), '
                        '"files_with_matches" shows file paths (default), "count" shows match counts.'
                    ),
                },
                "-A": {
                    "type": "integer",
                    "description": 'Number of lines to show after each match. Requires output_mode: "content".',
                },
                "-B": {
                    "type": "integer",
                    "description": 'Number of lines to show before each match. Requires output_mode: "content".',
                },
                "-C": {
                    "type": "integer",
                    "description": "Number of lines to show before and after each match.",
                },
                "-n": {
                    "type": "boolean",
                    "description": "Show line numbers in output. Defaults to true.",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case insensitive search",
                },
                "type": {
                    "type": "string",
                    "description": "File type to search (e.g., js, py, rust, go, java).",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Limit output to first N lines/entries. Defaults to 250.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip first N lines/entries before applying head_limit. Defaults to 0.",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline mode where . matches newlines. Default: false.",
                },
            },
            "required": ["pattern"],
        },
    },
}

_TYPE_MAP = {
    "js": ["*.js", "*.jsx", "*.mjs"],
    "ts": ["*.ts", "*.tsx", "*.mts"],
    "py": ["*.py", "*.pyi"],
    "rust": ["*.rs"],
    "go": ["*.go"],
    "java": ["*.java"],
    "c": ["*.c", "*.h"],
    "cpp": ["*.cpp", "*.cc", "*.cxx", "*.hpp", "*.h"],
    "rb": ["*.rb"],
    "swift": ["*.swift"],
    "kt": ["*.kt", "*.kts"],
}


def execute(args: dict[str, Any], *, validate_path=None, **_) -> str:
    import os

    pattern = args.get("pattern", "")
    search_path = args.get("path") or os.getcwd()
    glob_filter = args.get("glob")
    output_mode = args.get("output_mode", "files_with_matches")
    context_after = args.get("-A") or args.get("-C")
    context_before = args.get("-B") or args.get("-C")
    case_insensitive = args.get("-i", False)
    file_type = args.get("type")
    head_limit = args.get("head_limit", 250)
    offset = args.get("offset", 0)
    multiline = args.get("multiline", False)

    if validate_path:
        if err := validate_path("read", search_path):
            return err

    if shutil.which("rg") is not None:
        return _search_rg(
            pattern, search_path, glob_filter, output_mode,
            context_before, context_after, case_insensitive,
            file_type, head_limit, offset, multiline, validate_path,
        )
    return _search_grep(
        pattern, search_path, glob_filter, output_mode,
        context_before, context_after, case_insensitive,
        file_type, head_limit, offset, multiline, validate_path,
    )


def _filter_denied(lines: list[str], validate_path=None) -> list[str]:
    """Remove lines from files that are denied by read rules."""
    if not validate_path:
        return lines
    allowed_cache: dict[str, bool] = {}
    result = []
    for line in lines:
        file_path = line.split(":")[0] if ":" in line else line
        if file_path not in allowed_cache:
            allowed_cache[file_path] = validate_path("read", file_path) is None
        if allowed_cache[file_path]:
            result.append(line)
    return result


def _format_output(lines: list[str], output_mode: str, head_limit: int | None, offset: int | None) -> str:
    """Apply pagination and format with summary headers."""
    total = len(lines)

    if offset:
        lines = lines[offset:]
    if head_limit and head_limit > 0:
        lines = lines[:head_limit]

    limit_info = f"head_limit={head_limit}" if head_limit and total > head_limit else None

    if output_mode == "files_with_matches":
        n = len(lines)
        header = f"Found {n} file{'s' if n != 1 else ''}"
        if limit_info:
            header += f" ({limit_info})"
        return header + "\n" + "\n".join(lines)

    if output_mode == "count":
        total_matches = 0
        for l in lines:
            if not l:
                continue
            try:
                total_matches += int(l.split(":")[-1])
            except (ValueError, IndexError):
                continue
        n_files = len(lines)
        body = "\n".join(lines)
        suffix = f"\n\nFound {total_matches} total occurrence{'s' if total_matches != 1 else ''} across {n_files} file{'s' if n_files != 1 else ''}"
        if limit_info:
            suffix += f" ({limit_info})"
        return body + suffix

    # content mode
    body = "\n".join(lines)
    if limit_info:
        body += f"\n\n[Showing results with {limit_info}]"
    return body


def _build_type_glob(file_type: str | None) -> list[str]:
    """Convert a type shorthand to a list of --include globs for grep."""
    if not file_type or file_type not in _TYPE_MAP:
        return []
    return _TYPE_MAP[file_type]


def _search_rg(
    pattern: str, search_path: str, glob_filter: str | None,
    output_mode: str, context_before: int | None, context_after: int | None,
    case_insensitive: bool, file_type: str | None,
    head_limit: int | None, offset: int | None, multiline: bool,
    validate_path=None,
) -> str:
    cmd = ["rg"]
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        cmd.append("--line-number")
    if case_insensitive:
        cmd.append("-i")
    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])
    if context_before:
        cmd.extend(["-B", str(context_before)])
    if context_after:
        cmd.extend(["-A", str(context_after)])
    if glob_filter:
        cmd.extend(["--glob", glob_filter])
    if file_type:
        cmd.extend(["--type", file_type])
    cmd.extend(["--no-heading", "--color=never", pattern, search_path])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        if not output:
            return "No matches found."
        lines = output.split("\n")
        lines = _filter_denied(lines, validate_path)
        if not lines:
            return "No matches found."
        return _format_output(lines, output_mode, head_limit, offset)
    except subprocess.TimeoutExpired:
        return "<error>Search timed out</error>"
    except Exception as e:
        return f"<error>{e}</error>"


def _search_grep(
    pattern: str, search_path: str, glob_filter: str | None,
    output_mode: str, context_before: int | None, context_after: int | None,
    case_insensitive: bool, file_type: str | None,
    head_limit: int | None, offset: int | None, multiline: bool,
    validate_path=None,
) -> str:
    """Fallback using system grep when ripgrep is not available."""
    if multiline:
        return "<error>Multiline search requires ripgrep (rg). Please install ripgrep: https://github.com/BurntSushi/ripgrep</error>"

    cmd = ["grep", "-E"]

    if output_mode == "files_with_matches":
        cmd.append("-rl")
    elif output_mode == "count":
        cmd.append("-rc")
    else:
        cmd.append("-n")
    if case_insensitive:
        cmd.append("-i")
    if context_before:
        cmd.extend(["-B", str(context_before)])
    if context_after:
        cmd.extend(["-A", str(context_after)])

    # Type filtering via --include
    type_globs = _build_type_glob(file_type)
    for g in type_globs:
        cmd.extend(["--include", g])
    if glob_filter:
        cmd.extend(["--include", glob_filter])

    cmd.extend(["--color=never", pattern, "-r", search_path])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        if not output:
            return "No matches found."
        lines = output.split("\n")
        lines = _filter_denied(lines, validate_path)
        if not lines:
            return "No matches found."
        return _format_output(lines, output_mode, head_limit, offset)
    except subprocess.TimeoutExpired:
        return "<error>Search timed out</error>"
    except FileNotFoundError:
        return "<error>Neither ripgrep (rg) nor grep found. Please install ripgrep: https://github.com/BurntSushi/ripgrep</error>"
    except Exception as e:
        return f"<error>{e}</error>"
