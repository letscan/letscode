"""Tests for reasoning_content (thought) accumulation in MessageSubscriber.

Covers the fix for the DeepSeek thinking-mode contract: assistant history
messages must carry reasoning_content on tool-call turns (omitting it
returns HTTP 400). Thoughts are accumulated from agent_thought_chunk events
and attached to the assistant message at flush time. Feed replay rebuilds
them from logged events.

Background: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
"""

from letscode.subscribers import MessageSubscriber


class TestThoughtAccumulation:
    """agent_thought_chunk events accumulate into reasoning_content on the
    assistant message."""

    def test_thought_attached_to_plain_assistant_turn(self):
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "explain"}])
        sub("agent_thought_chunk", {"type": "text", "text": "thinking step 1"})
        sub("agent_thought_chunk", {"type": "text", "text": "thinking step 2"})
        sub("agent_message_chunk", {"type": "text", "text": "the answer"})
        sub.flush()
        msg = sub.messages[-1]
        assert msg["role"] == "assistant"
        assert msg["content"] == "the answer"
        assert msg["reasoning_content"] == "thinking step 1\nthinking step 2"

    def test_thought_attached_to_tool_call_turn(self):
        # The critical DeepSeek case: a turn with tool_calls MUST carry
        # reasoning_content, or the API returns 400 on the next request.
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "read file x"}])
        sub("agent_thought_chunk", {"type": "text", "text": "I should use Read"})
        sub("tool_call", {"toolCallId": "t1", "toolName": "Read", "rawInput": {"f": "x"}})
        sub("tool_call_update", {"toolCallId": "t1", "status": "completed",
                                 "rawOutput": "contents"})
        sub.flush()
        msg = sub.messages[-2]  # assistant (with tool_calls) is before tool result
        assert msg["role"] == "assistant"
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert msg["reasoning_content"] == "I should use Read"

    def test_no_thought_no_reasoning_field(self):
        # A turn with no thoughts must not add an empty reasoning_content
        # (would pollute the message for providers that reject empty fields).
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "hi"}])
        sub("agent_message_chunk", {"type": "text", "text": "hello"})
        sub.flush()
        msg = sub.messages[-1]
        assert "reasoning_content" not in msg

    def test_thought_only_turn_produces_message(self):
        # A turn that emitted only reasoning (no text/tool) should still
        # produce an assistant message carrying reasoning_content.
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "think silently"}])
        sub("agent_thought_chunk", {"type": "text", "text": "private reasoning"})
        sub.flush()
        msg = sub.messages[-1]
        assert msg["role"] == "assistant"
        assert msg["reasoning_content"] == "private reasoning"

    def test_thought_parts_cleared_after_flush(self):
        # State must reset between turns so reasoning doesn't leak across.
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "q1"}])
        sub("agent_thought_chunk", {"type": "text", "text": "thought A"})
        sub("agent_message_chunk", {"type": "text", "text": "answer A"})
        sub.flush()
        # Second turn with no thought.
        sub("prompt", [{"type": "text", "text": "q2"}])
        sub("agent_message_chunk", {"type": "text", "text": "answer B"})
        sub.flush()
        last = sub.messages[-1]
        assert "reasoning_content" not in last  # no leakage from turn 1


class TestFeedReplayRebuildsThoughts:
    """Replaying a log with agent_thought_chunk events rebuilds
    reasoning_content in the messages list (feed.py uses MessageSubscriber)."""

    def test_replay_rebuilds_reasoning_content(self):
        # Simulate a logged turn: prompt → thought chunks → answer → result.
        events = [
            {"type": "prompt", "data": [{"type": "text", "text": "why?"}]},
            {"type": "agent_thought_chunk", "data": {"type": "text", "text": "reasoning line 1"}},
            {"type": "agent_thought_chunk", "data": {"type": "text", "text": "reasoning line 2"}},
            {"type": "agent_message_chunk", "data": {"type": "text", "text": "the answer"}},
            {"type": "result", "data": {"stopReason": "end_turn"}},
        ]
        sub = MessageSubscriber()
        for ev in events:
            sub(ev["type"], ev["data"])
        sub.flush()
        assistant = sub.messages[-1]
        assert assistant["reasoning_content"] == "reasoning line 1\nreasoning line 2"

    def test_legacy_log_without_thoughts_rebuilds_cleanly(self):
        # Old logs that never logged agent_thought_chunk must replay without
        # error and produce assistant messages with no reasoning_content.
        events = [
            {"type": "prompt", "data": [{"type": "text", "text": "hi"}]},
            {"type": "agent_message_chunk", "data": {"type": "text", "text": "hello"}},
            {"type": "result", "data": {"stopReason": "end_turn"}},
        ]
        sub = MessageSubscriber()
        for ev in events:
            sub(ev["type"], ev["data"])
        sub.flush()
        assistant = sub.messages[-1]
        assert "reasoning_content" not in assistant
        assert assistant["content"] == "hello"

    def test_multi_turn_with_interleaved_thoughts(self):
        # Two turns, each with its own reasoning — verify isolation.
        events = [
            {"type": "prompt", "data": [{"type": "text", "text": "q1"}]},
            {"type": "agent_thought_chunk", "data": {"type": "text", "text": "think 1"}},
            {"type": "agent_message_chunk", "data": {"type": "text", "text": "a1"}},
            {"type": "prompt", "data": [{"type": "text", "text": "q2"}]},
            {"type": "agent_thought_chunk", "data": {"type": "text", "text": "think 2"}},
            {"type": "agent_message_chunk", "data": {"type": "text", "text": "a2"}},
        ]
        sub = MessageSubscriber()
        for ev in events:
            sub(ev["type"], ev["data"])
        sub.flush()
        # Two assistant messages, each with its own reasoning.
        assistants = [m for m in sub.messages if m["role"] == "assistant"]
        assert len(assistants) == 2
        assert assistants[0]["reasoning_content"] == "think 1"
        assert assistants[1]["reasoning_content"] == "think 2"
