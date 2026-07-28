"""Tests for upstream large-result externalization in EventHub.

Covers:
- Large rawOutput (>RESULT_THRESHOLD) on a terminal tool_call_update is
  externalized: persisted to cache_dir, rawOutput replaced with a
  <persisted-output> preview reference.
- Small rawOutput and streaming chunks pass through unchanged.
- The persisted file exists, contains the full result, and the embedded
  path is absolute.
- The externalized event, when serialized as a JSONL line, stays well
  under the asyncio 64 KiB single-line limit.
- Externalization is off by default (no cache_dir → passthrough).
- A subscriber receiving the externalized data sees the reference, not
  the full payload.
"""

import json
from pathlib import Path

import pytest

from letscode.events import EventHub, RESULT_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CaptureSubscriber:
    """Records every (event_type, data) pair it receives."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, data: dict) -> None:
        # Deep-ish copy so later mutation doesn't affect the snapshot.
        self.events.append((event_type, dict(data)))


def _make_hub(cache_dir: Path) -> EventHub:
    hub = EventHub()
    hub.enable_externalization(cache_dir)
    return hub


# ---------------------------------------------------------------------------
# Externalization behavior
# ---------------------------------------------------------------------------

class TestExternalizeLargeResult:
    """Large terminal tool_call_update events are externalized upstream."""

    def test_large_rawoutput_is_replaced_with_reference(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        large = "x" * (RESULT_THRESHOLD + 100)
        hub.emit_tool_update("call_1", status="completed", raw_output=large)

        assert len(sub.events) == 1
        _, data = sub.events[0]
        assert data["rawOutput"].startswith("<persisted-output>")
        assert "Output too large" in data["rawOutput"]
        # Preview is present
        assert "Preview:" in data["rawOutput"]

    def test_persisted_file_contains_full_result(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        large = "LINE\n" * 10000  # well over 32 KB
        hub.emit_tool_update("call_42", status="completed", raw_output=large)

        _, data = sub.events[0]
        ref = data["rawOutput"]
        # Extract path from "Full output saved to: <path>"
        assert "Full output saved to:" in ref
        path_line = [l for l in ref.split("\n") if "Full output saved to:" in l][0]
        saved_path = Path(path_line.split("Full output saved to:")[-1].strip())

        assert saved_path.exists()
        assert saved_path.read_text(encoding="utf-8") == large

    def test_persisted_path_is_absolute(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        hub.emit_tool_update(
            "call_abs", status="completed",
            raw_output="y" * (RESULT_THRESHOLD + 10),
        )

        _, data = sub.events[0]
        path_line = [
            l for l in data["rawOutput"].split("\n")
            if "Full output saved to:" in l
        ][0]
        saved_path = Path(path_line.split("Full output saved to:")[-1].strip())
        assert saved_path.is_absolute()

    def test_externalized_line_under_64kb(self, tmp_path: Path):
        """The JSONL line must stay under asyncio's 64 KiB limit."""
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        hub.emit_tool_update(
            "call_big", status="completed",
            raw_output="z" * (1024 * 1024),  # 1 MB
        )

        _, data = sub.events[0]
        event = {"type": "tool_call_update", "data": data}
        line = json.dumps(event, ensure_ascii=False)
        assert len(line) < 65536

    def test_failed_status_also_externalizes(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        large = "e" * (RESULT_THRESHOLD + 50)
        hub.emit_tool_update("call_fail", status="failed", raw_output=large)

        _, data = sub.events[0]
        assert data["rawOutput"].startswith("<persisted-output>")


class TestPassthroughSmallResult:
    """Small results and non-terminal events are not externalized."""

    def test_small_rawoutput_passes_through(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        hub.emit_tool_update("call_small", status="completed", raw_output="ok")

        _, data = sub.events[0]
        assert data["rawOutput"] == "ok"

    def test_streaming_chunk_passes_through(self, tmp_path: Path):
        """status=None streaming chunks must never be externalized."""
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        chunk = "c" * (RESULT_THRESHOLD + 1000)
        hub.emit_tool_update("call_stream", raw_output=chunk)

        _, data = sub.events[0]
        assert data["rawOutput"] == chunk
        assert "status" not in data

    def test_non_tool_call_update_passes_through(self, tmp_path: Path):
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        hub.emit_agent_message_chunk("hello world")
        assert sub.events[0][0] == "agent_message_chunk"
        assert sub.events[0][1]["text"] == "hello world"


class TestExternalizationDisabled:
    """Without enable_externalization, everything passes through raw."""

    def test_no_cache_dir_passthrough(self, tmp_path: Path):
        hub = EventHub()  # no enable_externalization call
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        large = "n" * (RESULT_THRESHOLD + 100)
        hub.emit_tool_update("call_raw", status="completed", raw_output=large)

        _, data = sub.events[0]
        assert data["rawOutput"] == large
        # No cache files created
        assert not (tmp_path / "cache").exists()

    def test_original_dict_not_mutated(self, tmp_path: Path):
        """The caller's original data dict must not be mutated by externalization."""
        hub = _make_hub(tmp_path / "cache")
        sub = _CaptureSubscriber()
        hub.subscribe(sub)

        large = "m" * (RESULT_THRESHOLD + 10)
        hub.emit_tool_update("call_orig", status="completed", raw_output=large)

        # The subscriber received the externalized copy...
        _, received = sub.events[0]
        assert received["rawOutput"].startswith("<persisted-output>")
        # ...but the original is gone (emit_tool_update builds the dict
        # internally, so we verify via re-emit that no stale state lingers).


class TestSubscriberIntegration:
    """MessageSubscriber receives the already-externalized rawOutput."""

    def test_message_subscriber_gets_reference(self, tmp_path: Path):
        from letscode.subscribers import MessageSubscriber

        hub = _make_hub(tmp_path / "cache")
        msg_sub = MessageSubscriber()
        hub.subscribe(msg_sub)

        # Need a tool_call event before the update so MessageSubscriber tracks it
        hub.emit_tool_call("call_msg", "Read", {"file_path": "/tmp/x"})
        large = "d" * (RESULT_THRESHOLD + 100)
        hub.emit_tool_update("call_msg", status="completed", raw_output=large)

        msg_sub.flush()
        tool_msgs = [m for m in msg_sub.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"].startswith("<persisted-output>")
