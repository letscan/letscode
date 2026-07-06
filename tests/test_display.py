"""Tests for verbose-mode display formatting."""

import os
import sys

from letscode.tools._display import (
    _detect_ansi,
    format_call,
    format_result,
    reset_ansi_cache,
    _dim,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockTty:
    def isatty(self):
        return True


class _MockPipe:
    def isatty(self):
        return False


def _with_env(env, func):
    old = {}
    for k in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        old[k] = os.environ.pop(k, None)
    try:
        os.environ.update({k: v for k, v in env.items() if v is not None})
        reset_ansi_cache()
        return func()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_ansi_cache()


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


# ---------------------------------------------------------------------------
# ANSI detection
# ---------------------------------------------------------------------------


class TestAnsiDetection:
    def test_no_color_disables(self):
        assert _with_env({"NO_COLOR": "1"}, lambda: _detect_ansi()) is False

    def test_force_color_enables(self):
        old = sys.stderr
        sys.stderr = _MockPipe()
        try:
            assert _with_env({"FORCE_COLOR": "1"}, lambda: _detect_ansi()) is True
        finally:
            sys.stderr = old

    def test_term_dumb_disables(self):
        old = sys.stderr
        sys.stderr = _MockTty()
        try:
            assert _with_env({"TERM": "dumb"}, lambda: _detect_ansi()) is False
        finally:
            sys.stderr = old

    def test_pipe_disables(self):
        old = sys.stderr
        sys.stderr = _MockPipe()
        try:
            assert _with_env({}, lambda: _detect_ansi()) is False
        finally:
            sys.stderr = old

    def test_tty_enables(self):
        old = sys.stderr
        sys.stderr = _MockTty()
        try:
            assert _with_env({}, lambda: _detect_ansi()) is True
        finally:
            sys.stderr = old

    def test_no_color_overrides_force_color(self):
        assert _with_env({"NO_COLOR": "1", "FORCE_COLOR": "1"}, lambda: _detect_ansi()) is False


# ---------------------------------------------------------------------------
# Dim wrapper
# ---------------------------------------------------------------------------


class TestDim:
    def test_ansi_off_returns_plain(self):
        result = _with_env({"NO_COLOR": "1"}, lambda: _dim("hello"))
        assert result == "hello"

    def test_ansi_on_wraps_dim(self):
        old = sys.stderr
        sys.stderr = _MockTty()
        try:
            result = _with_env({}, lambda: _dim("hello"))
            assert "hello" in result
            assert "\033[2m" in result
            assert "\033[0m" in result
        finally:
            sys.stderr = old


# ---------------------------------------------------------------------------
# Call formatters (ANSI off — plain text assertions)
# ---------------------------------------------------------------------------


class TestFormatCall:
    def _fmt(self, name, args):
        return _with_env({"NO_COLOR": "1"}, lambda: format_call(name, args))

    def test_bash_basic(self):
        assert self._fmt("Bash", {"command": "ls -la"}) == "-> Bash   ls -la"

    def test_bash_with_description(self):
        result = self._fmt("Bash", {"command": "ls", "description": "List files"})
        assert "List files" in result

    def test_read_basic(self):
        assert "agent.py" in self._fmt("Read", {"file_path": "src/agent.py"})

    def test_read_with_range(self):
        result = self._fmt("Read", {"file_path": "src/agent.py", "offset": 50, "limit": 10})
        assert "L50-59" in result

    def test_write(self):
        result = self._fmt("Write", {"file_path": "src/new.py", "content": "a\nb\nc"})
        assert "3 lines" in result

    def test_edit(self):
        result = self._fmt("Edit", {
            "file_path": "src/agent.py",
            "old_string": "def run_agent(",
            "new_string": "def run_loop(",
        })
        assert "run_agent" in result
        assert "run_loop" in result

    def test_edit_replace_all(self):
        result = self._fmt("Edit", {
            "file_path": "f.py", "old_string": "a", "new_string": "b", "replace_all": True,
        })
        assert "(all)" in result

    def test_glob(self):
        result = self._fmt("Glob", {"pattern": "**/*.py", "path": "src"})
        assert "**/*.py" in result
        assert "src" in result

    def test_grep(self):
        result = self._fmt("Grep", {"pattern": "TODO", "output_mode": "content"})
        assert "TODO" in result
        assert "content" in result

    def test_skill(self):
        result = self._fmt("Skill", {"skill": "commit", "args": "-m fix"})
        assert "/commit" in result

    def test_agent(self):
        result = self._fmt("Agent", {"description": "Explore code", "subagent_type": "Explore"})
        assert "Explore code" in result

    def test_mcp(self):
        result = self._fmt("mcp__playwright__navigate", {"url": "https://example.com"})
        assert "playwright/navigate" in result

    def test_unknown_tool(self):
        result = self._fmt("CustomTool", {})
        assert "CustomTool" in result


# ---------------------------------------------------------------------------
# Result formatters (ANSI off)
# ---------------------------------------------------------------------------


class TestFormatResult:
    def _fmt(self, name, result, success=True, args=None):
        return _with_env(
            {"NO_COLOR": "1"},
            lambda: format_result(name, result, success, args or {}),
        )

    def _has_dim_wrap(self, text):
        """Check that ANSI-on output wraps in dim."""
        return "\033[2m" in text and "\033[0m" in text

    # --- Bash ---

    def test_bash_short_output(self):
        r = self._fmt("Bash", "hello world")
        assert "1 lines" in r
        assert "hello world" in r

    def test_bash_multiline_preview(self):
        output = "\n".join(f"line {i}" for i in range(12))
        r = self._fmt("Bash", output)
        assert "12 lines" in r
        assert "2 lines omitted" in r

    def test_bash_error(self):
        r = self._fmt("Bash", "error: not found\n\n[Exit code: 1]", False)
        assert "FAIL" in r
        assert "Exit code" in r

    def test_bash_full_preview_when_short(self):
        r = self._fmt("Bash", "a\nb\nc")
        assert "3 lines" in r
        assert "omitted" not in r

    def test_bash_no_output_placeholder(self):
        r = self._fmt("Bash", "(no output)")
        assert "0 lines" in r
        assert "(No output)" in r

    def test_bash_empty_output(self):
        r = self._fmt("Bash", "")
        assert "(No output)" in r

    # --- Read ---

    def test_read_success(self):
        content = "\n".join(f"  {i}\tline" for i in range(1, 6))
        r = self._fmt("Read", content, True, {"offset": 1, "limit": 5})
        assert "5 lines" in r
        assert "(L1-5)" in r

    def test_read_error(self):
        r = self._fmt("Read", "<error>File not found: x.py</error>", False)
        assert "File not found" in r

    def test_read_long_output_preview(self):
        content = "\n".join(f"  {i}\tline content" for i in range(1, 21))
        r = self._fmt("Read", content, True, {})
        assert "20 lines" in r
        assert "10 lines omitted" in r

    def test_read_empty_file(self):
        r = self._fmt("Read", "", True, {})
        assert "(Empty)" in r
        assert "lines" not in r

    # --- Write ---

    def test_write_created(self):
        r = self._fmt("Write", "File created successfully at: x.py (3 lines)", True,
                       {"content": "a\nb\nc"})
        assert "created" in r
        assert "3 lines" in r

    def test_write_shows_content(self):
        r = self._fmt("Write", "File created successfully at: x.py (2 lines)", True,
                       {"content": "hello\nworld"})
        assert "hello" in r
        assert "world" in r

    def test_write_large_file_head_tail_preview(self):
        content = "\n".join(f"line{i}" for i in range(100))
        r = self._fmt("Write", "File created successfully at: big.py (100 lines)", True,
                       {"file_path": "/p/big.py", "content": content})
        assert "100 lines" in r
        assert "chars" in r
        assert "/p/big.py" in r
        # head + tail with omission
        assert "line0" in r
        assert "line99" in r
        assert "50 lines omitted" in r
        # middle lines are not shown
        assert "line50" not in r

    # --- Edit ---

    def test_edit_success(self):
        r = self._fmt("Edit", "The file x.py has been updated. (2 lines -> 3 lines)", True,
                       {"file_path": "x.py", "old_string": "a\nb", "new_string": "a\nb\nc"})
        assert "2 -> 3" in r
        assert "- a" in r
        assert "- b" in r
        assert "+ a" in r

    def test_edit_replace_all(self):
        r = self._fmt("Edit",
                       "The file x.py has been updated. Replaced 3 occurrences (1 lines -> 1 lines each).",
                       True,
                       {"file_path": "x.py", "old_string": "a", "new_string": "b", "replace_all": True})
        assert "Replaced 3" in r

    def test_edit_error(self):
        r = self._fmt("Edit", "<error>old_string not found in x.py</error>", False)
        assert "old_string not found" in r

    # --- Glob ---

    def test_glob_files(self):
        r = self._fmt("Glob", "src/a.py\nsrc/b.py")
        assert "2 files" in r

    def test_glob_no_files(self):
        r = self._fmt("Glob", "No files found")
        assert "0 files" in r

    def test_glob_truncated(self):
        files = "\n".join(f"file_{i}.py" for i in range(20))
        r = self._fmt("Glob", files + "\n\n(truncated)")
        assert "20 files" in r
        assert "truncated" in r

    def test_glob_short_shows_all(self):
        r = self._fmt("Glob", "a.py\nb.py\nc.py")
        assert "3 files" in r
        assert "a.py" in r
        assert "c.py" in r

    # --- Grep ---

    def test_grep_found_files(self):
        r = self._fmt("Grep", "Found 5 files\nsrc/a.py\nsrc/b.py")
        assert "5 files" in r

    def test_grep_no_matches(self):
        r = self._fmt("Grep", "No matches found.")
        assert "no matches" in r

    def test_grep_count_mode(self):
        r = self._fmt("Grep", "src/a.py:10\nsrc/b.py:5\n\nFound 15 total occurrences across 2 files")
        assert "15 matches" in r
        assert "2 files" in r

    # --- Skill ---

    def test_skill_success(self):
        # Skill execute() returns a display label carrying name + path
        r = self._fmt("Skill", "Loaded skill commit from /p/commit/SKILL.md", True,
                       {"skill": "commit"})
        assert "Loaded skill" in r
        assert "commit" in r
        assert "/p/commit/SKILL.md" in r

    # --- Agent ---

    def test_agent_success(self):
        r = self._fmt("Agent", "result text")
        assert "completed" in r

    def test_agent_error(self):
        r = self._fmt("Agent", "<error>Sub-agent timed out (300s)</error>", False)
        assert "timed out" in r

    # --- MCP ---

    def test_mcp_success(self):
        r = self._fmt("mcp__test__tool", "result line")
        assert "result line" in r

    def test_mcp_error(self):
        r = self._fmt("mcp__test__tool", "<error>something failed</error>", False)
        assert "something failed" in r

    # --- Default ---

    def test_unknown_tool_success(self):
        r = self._fmt("Unknown", "some output")
        assert "some output" in r

    def test_unknown_tool_error(self):
        r = self._fmt("Unknown", "<error>bad</error>", False)
        assert "bad" in r

    # --- Dim wrap ---

    def test_call_has_bold_not_dim(self):
        old = sys.stderr
        sys.stderr = _MockTty()
        try:
            result = _with_env({}, lambda: format_call("Bash", {"command": "ls"}))
            assert "\033[1m" in result   # bold
            assert "\033[2m" not in result  # no dim
        finally:
            sys.stderr = old

    def test_result_has_dim_wrap(self):
        old = sys.stderr
        sys.stderr = _MockTty()
        try:
            result = _with_env({}, lambda: format_result("Bash", "output", True, {}))
            assert self._has_dim_wrap(result)
        finally:
            sys.stderr = old

    def test_no_dim_in_plain_mode(self):
        result = _with_env({"NO_COLOR": "1"}, lambda: format_result("Bash", "output", True, {}))
        assert "\033[" not in result


class TestResultFooterCacheRate:
    """The stderr stat footer shows the cache hit rate inline next to the
    input token count when the turn hit cache; omits it otherwise. Format:

      📊 tokens: 906 (98%cached) in / 48 out / 954 total
    """

    def _footer(self, usage: dict) -> str:
        """Render the footer for a fake result event and return the stripped text."""
        from letscode.subscribers import CliOutputSubscriber
        sub = _with_env({"NO_COLOR": "1"}, lambda: CliOutputSubscriber())

        import io
        buf = io.StringIO()
        old = sys.stderr
        sys.stderr = buf
        try:
            sub("result", {"stopReason": "end_turn", "usage": usage})
        finally:
            sys.stderr = old
        return _strip_ansi(buf.getvalue()).strip()

    def test_no_rate_when_no_cache(self):
        out = self._footer({"prompt_tokens": 906, "completion_tokens": 48,
                            "total_tokens": 954, "cache_read_tokens": 0})
        assert "cached" not in out
        assert "906 in" in out and "48 out" in out

    def test_cache_rate_inline(self):
        # 896 of 906 prompt tokens cached → 98%.
        out = self._footer({"prompt_tokens": 906, "completion_tokens": 48,
                            "total_tokens": 954, "cache_read_tokens": 896})
        assert "(98%cached)" in out

    def test_cache_rate_floors_to_zero(self):
        # 1 of 1000 — rounds down to 0%, still shown because cache_read > 0.
        out = self._footer({"prompt_tokens": 1000, "completion_tokens": 10,
                            "total_tokens": 1010, "cache_read_tokens": 1})
        assert "(0%cached)" in out

