"""Verbose-mode display formatting for tool calls and results."""

import os
import re
import sys

# ---------------------------------------------------------------------------
# ANSI capability detection
# ---------------------------------------------------------------------------

_ANSI: bool | None = None


def use_ansi() -> bool:
    global _ANSI
    if _ANSI is None:
        _ANSI = _detect_ansi()
    return _ANSI


def reset_ansi_cache() -> None:
    global _ANSI
    _ANSI = None


def _detect_ansi() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") in ("1", "true", "yes"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


# ---------------------------------------------------------------------------
# SGR constants & helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_CHECK = "✓"   # ✓
_CROSS = "✗"   # ✗
_ARROW = "→"   # →
_BAR = "│"     # │

_ASCII_CHECK = "ok"
_ASCII_CROSS = "FAIL"
_ASCII_ARROW = "->"
_ASCII_BAR = "|"


def _dim(text: str) -> str:
    """Wrap text in dim when ANSI is available."""
    if not use_ansi():
        return text
    return _DIM + text + _RESET


def _sym(unicode: str, ascii: str) -> str:
    return unicode if use_ansi() else ascii


# ---------------------------------------------------------------------------
# Preview helpers
# ---------------------------------------------------------------------------

def _preview_lines(lines: list[str], n: int = 5) -> list[str]:
    bar = _sym(_BAR, _ASCII_BAR)
    if len(lines) <= 2 * n:
        return [f"  {bar} {l}" for l in lines]
    head = [f"  {bar} {l}" for l in lines[:n]]
    omitted = len(lines) - 2 * n
    tail = [f"  {bar} {l}" for l in lines[-n:]]
    return head + [f"  ... ({omitted} lines omitted)"] + tail


def _preview_items(items: list[str], n: int = 5) -> list[str]:
    bar = _sym(_BAR, _ASCII_BAR)
    if len(items) <= 2 * n:
        return [f"  {bar} {l}" for l in items]
    head = [f"  {bar} {l}" for l in items[:n]]
    remaining = len(items) - n
    return head + [f"  ... ({remaining} more)"]


# ---------------------------------------------------------------------------
# Status prefix
# ---------------------------------------------------------------------------


def _status(success: bool) -> str:
    if success:
        return _sym(_CHECK, _ASCII_CHECK) + " "
    return _sym(_CROSS, _ASCII_CROSS) + " "


# ---------------------------------------------------------------------------
# Call formatters
# ---------------------------------------------------------------------------


def _bold(text: str) -> str:
    if not use_ansi():
        return text
    return _BOLD + text + _RESET


def format_call(name: str, args: dict) -> str:
    arrow = _sym(_ARROW, _ASCII_ARROW)
    prefix = f"{arrow} {_bold(name)}"
    formatter = _CALL_FORMATTERS.get(name)
    if formatter:
        return f"{prefix}   {formatter(args)}"
    if name.startswith("mcp__"):
        return f"{prefix}   {_call_mcp(name, args)}"
    return prefix


def _call_bash(args: dict) -> str:
    cmd = args.get("command", "")
    first_line = cmd.split("\n")[0][:60]
    desc = args.get("description")
    parts = [first_line]
    if desc:
        parts.append(f"({desc})")
    return "  ".join(parts)


def _call_read(args: dict) -> str:
    fp = args.get("file_path", "")
    parts = [fp]
    offset = args.get("offset")
    limit = args.get("limit")
    if offset or limit:
        start = offset or "?"
        end = (offset + limit - 1) if offset and limit else "?"
        parts.append(f"(L{start}-{end})")
    return "  ".join(parts)


def _call_write(args: dict) -> str:
    fp = args.get("file_path", "")
    content = args.get("content", "")
    n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"{fp}  ({n_lines} lines)"


def _call_edit(args: dict) -> str:
    fp = args.get("file_path", "")
    old = args.get("old_string", "").split("\n")[0][:40]
    new = args.get("new_string", "").split("\n")[0][:40]
    suffix = "  (all)" if args.get("replace_all") else ""
    return f'{fp}  "{old}" -> "{new}"{suffix}'


def _call_glob(args: dict) -> str:
    pat = args.get("pattern", "")
    loc = f"  in {args['path']}" if args.get("path") else ""
    return f"{pat}{loc}"


def _call_grep(args: dict) -> str:
    pat = args.get("pattern", "")
    mode = args.get("output_mode", "files_with_matches")
    loc = f"  in {args['path']}" if args.get("path") else ""
    return f"{pat}  ({mode}){loc}"


def _call_skill(args: dict) -> str:
    name = args.get("skill", "")
    a = f"  {args['args']}" if args.get("args") else ""
    return f"/{name}{a}"


def _call_agent(args: dict) -> str:
    desc = args.get("description", "")
    sub = args.get("subagent_type", "")
    sub_suffix = f"  ({sub})" if sub else ""
    return f"{desc}{sub_suffix}"


def _call_mcp(name: str, args: dict) -> str:
    parts = name.split("__", 2)
    if len(parts) >= 3:
        label = f"{parts[1]}/{parts[2]}"
    else:
        label = name
    for k, v in args.items():
        if isinstance(v, str) and len(v) < 60:
            return f"{label}  {v}"
        break
    return label


_CALL_FORMATTERS = {
    "Bash": _call_bash,
    "Read": _call_read,
    "Write": _call_write,
    "Edit": _call_edit,
    "Glob": _call_glob,
    "Grep": _call_grep,
    "Skill": _call_skill,
    "Agent": _call_agent,
}

# ---------------------------------------------------------------------------
# Result formatters
# ---------------------------------------------------------------------------


def format_result(name: str, result: str, success: bool, args: dict) -> str:
    formatter = _RESULT_FORMATTERS.get(name)
    if formatter:
        raw = formatter(result, success, args)
    elif name.startswith("mcp__"):
        raw = _result_mcp(result, success)
    else:
        raw = _result_default(result, success)
    return _dim(raw)


def _result_bash(result: str, success: bool, args: dict) -> str:
    stripped = result.strip()
    exit_code = None
    body = stripped
    # Strip trailing exit-code marker (present on failures)
    tail_nl = body.rfind("\n")
    if tail_nl != -1 and body[tail_nl + 1:].startswith("[Exit code:"):
        exit_code = body[tail_nl + 1:]
        body = body[:tail_nl].rstrip()
    elif body.startswith("[Exit code:"):
        exit_code = body
        body = ""

    # No meaningful output (empty, or the tool's "(no output)" placeholder)
    body_empty = (not body) or body == "(no output)"

    if body_empty:
        if not success:
            ec = exit_code.strip("[]") if exit_code else "failed"
            return f"{_status(False)}{ec}  (No output)"
        return f"{_status(True)}0 lines  (No output)"

    lines = body.split("\n")
    if not success:
        ec = exit_code.strip("[]") if exit_code else "failed"
        header = f"{_status(False)}{ec}"
    else:
        header = f"{_status(True)}{len(lines)} lines"

    preview = _preview_lines(lines)
    return header + "\n" + "\n".join(preview)


def _result_read(result: str, success: bool, args: dict) -> str:
    if not success:
        return _format_error(result)

    stripped = result.strip()
    offset = args.get("offset")
    limit = args.get("limit")

    if not stripped:
        start = offset or 1
        end = limit if (offset and limit) else start
        rng = f"  (L{start}-{end})" if (offset or limit) else ""
        return f"{_status(True)}(Empty){rng}"

    lines = stripped.split("\n")
    start = offset or 1
    end = start + len(lines) - 1

    header = f"{_status(True)}{len(lines)} lines  (L{start}-{end})"

    preview = _preview_lines(lines)
    return header + "\n" + "\n".join(preview)


def _result_write(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)

    content = args.get("content", "")
    fp = args.get("file_path", "")
    lines = content.split("\n")
    n_lines = len(lines)
    n_chars = len(content)
    action = "created" if "created" in result.lower() else "updated"
    header = f"{_status(True)}{action}  {n_lines} lines / {n_chars} chars  {fp}"

    preview = _preview_lines(lines, n=25)
    return header + "\n" + "\n".join(preview)


def _result_edit(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)

    old_str = args.get("old_string", "")
    new_str = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    if replace_all:
        match = re.search(r"Replaced (\d+) occurrence", result)
        count = int(match.group(1)) if match else "?"
        summary = f"Replaced {count} occurrences"
    else:
        old_lines = old_str.count("\n") + 1 if old_str else 0
        new_lines = new_str.count("\n") + 1 if new_str else 0
        summary = f"{old_lines} -> {new_lines} lines"

    fp = args.get("file_path", "")
    header = f"{_status(True)}{summary}  {fp}"

    bar = _sym(_BAR, _ASCII_BAR)
    old_lines_list = old_str.split("\n") if old_str else []
    new_lines_list = new_str.split("\n") if new_str else []
    diff_parts: list[str] = []
    for l in old_lines_list:
        diff_parts.append(f"  {bar} - {l}")
    for l in new_lines_list:
        diff_parts.append(f"  {bar} + {l}")

    return header + "\n" + "\n".join(diff_parts)


def _result_glob(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)

    if result.strip() == "No files found":
        return f"{_status(True)}0 files"

    lines = result.strip().split("\n")
    files = [l for l in lines if l.strip() and not l.startswith("(")]
    truncated = "truncated" in result

    trunc = " (truncated)" if truncated else ""
    header = f"{_status(True)}{len(files)} files{trunc}"

    preview = _preview_items(files)
    return header + "\n" + "\n".join(preview)


def _result_grep(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)

    if result.startswith("No matches"):
        return f"{_status(True)}no matches"

    m_total = re.search(r"Found (\d+) total occurrences? across (\d+) files?", result)
    if m_total:
        return f"{_status(True)}{m_total.group(1)} matches in {m_total.group(2)} files"

    m_files = re.match(r"Found (\d+) files?", result)
    if m_files:
        return f"{_status(True)}{m_files.group(1)} files"

    lines = result.strip().split("\n")
    return f"{_status(True)}{len(lines)} lines"


def _result_skill(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)
    name = args.get("skill", "").lstrip("/")
    return f"{_status(True)}Loaded skill {name}"


def _result_agent(result: str, success: bool, args: dict, **_) -> str:
    if not success:
        return _format_error(result)
    return f"{_status(True)}completed"


def _result_mcp(result: str, success: bool) -> str:
    if not success or result.startswith("<error>"):
        return _format_error(result)
    lines = result.strip().split("\n")
    if len(lines) <= 1:
        first = lines[0][:80] if lines else "(no output)"
        return f"{_status(True)}{first}"
    return f"{_status(True)}{len(lines)} lines"


def _result_default(result: str, success: bool) -> str:
    if not success or result.startswith("<error>"):
        return _format_error(result)
    first = result.strip().split("\n")[0][:80]
    return f"{_status(True)}{first}"


def _format_error(result: str) -> str:
    msg = result.removeprefix("<error>").removesuffix("</error>").strip()
    return f"{_status(False)}{msg}"


_RESULT_FORMATTERS = {
    "Bash": _result_bash,
    "Read": _result_read,
    "Write": _result_write,
    "Edit": _result_edit,
    "Glob": _result_glob,
    "Grep": _result_grep,
    "Skill": _result_skill,
    "Agent": _result_agent,
}
