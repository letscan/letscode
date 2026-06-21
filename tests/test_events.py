"""Tests for LogSubscriber format and feed isolation.

LogSubscriber writes a human-readable, non-JSON debug log. This is a
deliberate design choice: the format must NOT be parseable as jsonl so
that read_events (the feed loader) structurally cannot mistake an internal
log for a replay feed. The replay feed is a separate file produced by
FeedOutputSubscriber (--output).
"""

import json
from pathlib import Path

import pytest

from letscode.events import EventHub, LogSubscriber, set_hub
from letscode.feed_util import read_events


class TestLogSubscriberFormat:
    """The log is human-readable text, not jsonl — feed isolation."""

    def _new_log(self, tmp_path) -> tuple[LogSubscriber, Path]:
        log = LogSubscriber(tmp_path / "logs")
        return log, log.log_path

    def test_file_extension_is_log(self, tmp_path):
        _, path = self._new_log(tmp_path)
        assert path.suffix == ".log"
        assert path.suffix != ".jsonl"

    def test_log_lines_are_not_valid_json(self, tmp_path):
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "hello"}])

        content = path.read_text().strip()
        for line in content.split("\n"):
            # Each line must FAIL json.loads — this is the feed-isolation
            # contract. If a line ever parses as JSON, a log could be
            # mistaken for a feed.
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)

    def test_read_events_rejects_log_file(self, tmp_path):
        """read_events (the feed loader) must fail on a log file."""
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "hello"}])

        with pytest.raises(json.JSONDecodeError):
            read_events(str(path))

    def test_format_is_timestamp_level_type_summary(self, tmp_path):
        log, path = self._new_log(tmp_path)
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        hub.emit_prompt(prompt_blocks=[{"type": "text", "text": "run tests"}])

        line = path.read_text().strip().split("\n")[0]
        # [ISO_TS] INFO event_type: summary
        assert line.startswith("[")
        assert "] INFO " in line
        assert "prompt: run tests" in line


class TestLogSubscriberSummaries:
    """Large tool outputs are summarized (size/lines), not written in full."""

    def _setup(self, tmp_path):
        log = LogSubscriber(tmp_path / "logs")
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(log)
        return log, hub

    def test_large_result_summarized(self, tmp_path):
        log, hub = self._setup(tmp_path)
        big = "LINE\n" * 5000  # ~25KB
        hub.emit_tool_call("t1", "Bash", {"command": "big"})
        hub.emit_tool_update("t1", status="completed", raw_output=big)

        content = log.log_path.read_text()
        # The full 25KB must NOT appear; only a line/byte summary
        assert big not in content
        assert "completed" in content
        assert "bytes" in content

    def test_tool_call_shows_name_and_args(self, tmp_path):
        log, hub = self._setup(tmp_path)
        hub.emit_tool_call("t1", "Read", {"file_path": "x.py"})

        content = log.log_path.read_text()
        assert "Read" in content
        assert "x.py" in content

    def test_agent_message_text_present(self, tmp_path):
        log, hub = self._setup(tmp_path)
        hub.emit_agent_message_chunk("working on it")

        content = log.log_path.read_text()
        assert "working on it" in content

    def test_long_summary_truncated(self, tmp_path):
        log, hub = self._setup(tmp_path)
        long_text = "x" * 1000
        hub.emit_agent_message_chunk(long_text)

        content = log.log_path.read_text()
        # Should be truncated (ellipsis marker), not the full 1000 chars
        assert "…" in content
        assert long_text not in content

    def test_log_debug_writes_debug_level(self, tmp_path):
        log = LogSubscriber(tmp_path / "logs")
        log.log_debug("something happened")

        content = log.log_path.read_text()
        assert "] DEBUG " in content
        assert "something happened" in content
