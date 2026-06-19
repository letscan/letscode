"""Tests for StreamBuffer and Bash record separator handling."""

import asyncio

from letscode.subscribers import StreamBuffer
from letscode.tools.bash import _read_records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_records(data: bytes) -> list[tuple[str, str]]:
    """Feed bytes into an asyncio.StreamReader and collect _read_records output."""
    async def run():
        reader = asyncio.StreamReader()
        # Pre-feed all data, then signal EOF
        reader.feed_data(data)
        reader.feed_eof()
        return [r async for r in _read_records(reader)]
    return asyncio.new_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# StreamBuffer — \r / \n semantics
# ---------------------------------------------------------------------------

class TestStreamBufferFeed:
    def test_newline_commits_lines(self):
        b = StreamBuffer()
        b.feed("alpha", "\n")
        b.feed("beta", "\n")
        assert b.all_lines == ["alpha", "beta"]
        assert b.merged == "alpha\nbeta"

    def test_carriage_return_overwrites_current_line(self):
        b = StreamBuffer()
        b.feed("loading 10%", "\r")
        b.feed("loading 50%", "\r")
        b.feed("loading 99%", "\r")
        # \r never commits; each feed at line start resets _current
        assert b.all_lines == ["loading 99%"]
        assert b.merged == "loading 99%"

    def test_progress_then_finalize_with_newline(self):
        b = StreamBuffer()
        b.feed("step 1", "\n")
        for pct in (25, 50, 75, 100):
            b.feed(f"progress {pct}%", "\r")
        b.feed("done", "\n")
        # The trailing \r in-progress line is overwritten by "done"
        assert b.all_lines == ["step 1", "done"]
        assert b.merged == "step 1\ndone"

    def test_empty(self):
        b = StreamBuffer()
        assert b.all_lines == []
        assert b.merged == ""
        assert b.preview() == ([], 0)


class TestStreamBufferPreview:
    def test_short_returns_all(self):
        b = StreamBuffer(head_tail=5)
        for i in range(8):
            b.feed(f"line{i}", "\n")
        lines, omitted = b.preview()
        assert lines == [f"line{i}" for i in range(8)]
        assert omitted == 0

    def test_long_returns_head_and_tail(self):
        b = StreamBuffer(head_tail=5)
        for i in range(13):
            b.feed(f"line{i}", "\n")
        lines, omitted = b.preview()
        assert lines == ["line0", "line1", "line2", "line3", "line4",
                         "line8", "line9", "line10", "line11", "line12"]
        assert omitted == 3  # 13 - 2*5

    def test_boundary_exactly_2n(self):
        b = StreamBuffer(head_tail=5)
        for i in range(10):
            b.feed(f"line{i}", "\n")
        lines, omitted = b.preview()
        assert len(lines) == 10
        assert omitted == 0


class TestStreamBufferMixedSeparators:
    def test_transient_progress_leaves_no_trace(self):
        # A \r-terminated progress line stays as the in-progress line and is
        # overwritten by the next \n-terminated real output.
        b = StreamBuffer()
        b.feed("fileA: ok", "\n")
        b.feed("fileB: ", "\n")
        b.feed("scanning...", "\r")
        b.feed("scanning... 100%", "\r")
        b.feed("fileC: ok", "\n")
        assert b.all_lines == ["fileA: ok", "fileB: ", "fileC: ok"]

    def test_trailing_carriage_return_preserved(self):
        b = StreamBuffer()
        b.feed("fileA: ok", "\n")
        b.feed("scanning... 100%", "\r")
        # A trailing \r line with no later real line IS the current line
        assert b.all_lines == ["fileA: ok", "scanning... 100%"]


# ---------------------------------------------------------------------------
# bash._read_records — separator classification
# ---------------------------------------------------------------------------

class TestReadRecords:
    def test_newline_separator(self):
        assert _collect_records(b"alpha\nbeta\n") == [("alpha", "\n"), ("beta", "\n")]

    def test_carriage_return_separator(self):
        assert _collect_records(b"p1\rp2\r") == [("p1", "\r"), ("p2", "\r")]

    def test_crlf_collapsed_to_newline(self):
        # \r\n must be treated as a single \n separator, not \r
        assert _collect_records(b"line\r\n") == [("line", "\n")]

    def test_mixed(self):
        data = b"a\nb\rc\r\nd\n"
        assert _collect_records(data) == [
            ("a", "\n"), ("b", "\r"), ("c", "\n"), ("d", "\n"),
        ]
