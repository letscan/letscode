"""Tests for load-time turn summarization."""

import asyncio
from unittest.mock import AsyncMock, patch

from letscode.acp.summaries import (
    SUMMARY_EVENT_TYPE, SUMMARY_TURN_THRESHOLD,
    find_summary_event, summarize_old_turns,
)
from letscode.stream import StreamResult


def _prompt(text):
    return {"type": "prompt", "data": [{"type": "text", "text": text}]}


def _agent_msg(text):
    return {"type": "agent_message_chunk", "data": {"text": text}}


def _turns(n):
    """Build a log with n turns (each = 1 prompt + 1 agent msg)."""
    events = []
    for i in range(n):
        events.append(_prompt(f"prompt {i}"))
        events.append(_agent_msg(f"answer {i}"))
    return events


class TestFindSummaryEvent:
    def test_finds_existing_summary(self):
        ev = [{"type": SUMMARY_EVENT_TYPE, "data": {"text": "s"}}]
        assert find_summary_event(ev) == ev[0]

    def test_returns_none_when_absent(self):
        assert find_summary_event([{"type": "prompt"}]) is None


class TestSummarizeOldTurns:
    def test_no_summary_when_under_threshold(self):
        events = _turns(5)
        async def run():
            return await summarize_old_turns(events, keep_count=20, model_id="m")
        assert asyncio.run(run()) is None

    def test_generates_summary_when_over_threshold(self):
        events = _turns(25)
        async def run():
            with patch("letscode.acp.summaries.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="early work summary", tool_calls=[])
            )):
                return await summarize_old_turns(events, keep_count=20, model_id="m")
        ev = asyncio.run(run())
        assert ev is not None
        assert ev["type"] == SUMMARY_EVENT_TYPE
        assert ev["data"]["text"] == "early work summary"
        assert ev["data"]["summarized_turns"] == 5   # 25 - 20

    def test_llm_failure_returns_none(self):
        events = _turns(25)
        async def run():
            with patch("letscode.acp.summaries.call_llm", new=AsyncMock(
                side_effect=RuntimeError("down")
            )):
                return await summarize_old_turns(events, keep_count=20, model_id="m")
        assert asyncio.run(run()) is None

    def test_empty_summary_returns_none(self):
        events = _turns(25)
        async def run():
            with patch("letscode.acp.summaries.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="", tool_calls=[])
            )):
                return await summarize_old_turns(events, keep_count=20, model_id="m")
        assert asyncio.run(run()) is None

    def test_threshold_constant(self):
        # Currently 3 for testing; restore to 20 later.
        assert SUMMARY_TURN_THRESHOLD == 20


class TestSessionSummarySkippedOnReplay:
    """The agent's --feed replay must ignore session/summary events."""

    def test_message_subscriber_ignores_session_summary(self):
        from letscode.subscribers import MessageSubscriber
        sub = MessageSubscriber()
        # Feed a summary event then a normal turn.
        sub(SUMMARY_EVENT_TYPE, {"text": "old summary", "summarized_turns": 5})
        sub("prompt", [{"type": "text", "text": "hi"}])
        sub("agent_message_chunk", {"type": "text", "text": "hello"})
        sub.flush()
        # The summary must NOT appear as a message; only the user+assistant turn.
        assert len(sub.messages) == 2
        assert sub.messages[0] == {"role": "user", "content": "hi"}
        assert sub.messages[1] == {"role": "assistant", "content": "hello"}
