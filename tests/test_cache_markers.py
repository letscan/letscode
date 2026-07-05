"""Tests for cache_control marker injection (letscode/cache_markers.py).

The strategy (``system_plus_rolling``) and its measurement basis are documented
in docs/cache-multiturn-probe-2026-07-06.md. These tests pin the contract:
which messages get marked, idempotency on feed-replay, marker-count guard, and
no-op behavior for auto-caching providers.
"""

from letscode.cache_markers import apply_cache_markers, _mark_last_block, _promote_to_blocks


# ---------------------------------------------------------------------------
# No-op modes
# ---------------------------------------------------------------------------

class TestNoOpModes:
    def test_auto_returns_unchanged_messages(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = apply_cache_markers(msgs, "auto")
        assert out == msgs
        # And the original is not mutated.
        assert msgs[0]["content"] == "sys"

    def test_none_returns_unchanged_messages(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out = apply_cache_markers(msgs, "none")
        assert out == msgs

    def test_auto_does_not_mutate_input(self):
        msgs = [{"role": "system", "content": "sys"}]
        apply_cache_markers(msgs, "auto")
        assert msgs[0]["content"] == "sys"  # untouched


# ---------------------------------------------------------------------------
# Explicit mode — system message
# ---------------------------------------------------------------------------

class TestSystemMarking:
    def test_explicit_promotes_system_to_blocks_and_marks(self):
        msgs = [{"role": "system", "content": "you are an agent"},
                {"role": "user", "content": "hi"}]
        out = apply_cache_markers(msgs, "explicit")
        sys_content = out[0]["content"]
        assert isinstance(sys_content, list)
        assert sys_content[-1]["cache_control"] == {"type": "ephemeral"}
        assert sys_content[-1]["text"] == "you are an agent"

    def test_explicit_does_not_mutate_input_system(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        apply_cache_markers(msgs, "explicit")
        assert msgs[0]["content"] == "sys"  # caller's copy unchanged

    def test_system_without_role_system_not_marked_at_index_zero(self):
        # Defensive: if messages[0] isn't a system message, we don't blindly
        # mark it. (Doesn't happen in letscode's agent loop, but guards callers.)
        msgs = [{"role": "user", "content": "first"}, {"role": "user", "content": "second"}]
        out = apply_cache_markers(msgs, "explicit")
        # No system at index 0, so only the rolling pair (both users) get marked.
        assert out[0]["content"][-1].get("cache_control") == {"type": "ephemeral"}
        assert out[1]["content"][-1].get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Explicit mode — rolling pair
# ---------------------------------------------------------------------------

class TestRollingPair:
    def test_last_two_non_system_marked(self):
        # system + 3 history messages: the last two (u3, a3) get marked.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ]
        out = apply_cache_markers(msgs, "explicit")
        # system (idx 0), u3 (idx 5), a3 (idx 6) marked.
        assert out[0]["content"][-1].get("cache_control")
        assert out[5]["content"][-1].get("cache_control")
        assert out[6]["content"][-1].get("cache_control")
        # u1, a1, u2, a2 (idx 1-4) NOT marked.
        for i in (1, 2, 3, 4):
            assert not _has_marker(out[i])

    def test_only_one_non_system_message_marks_just_it(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "only"}]
        out = apply_cache_markers(msgs, "explicit")
        assert out[0]["content"][-1].get("cache_control")
        assert out[1]["content"][-1].get("cache_control")

    def test_only_system_message_marks_just_system(self):
        # No history at all — only the system breakpoint fires.
        msgs = [{"role": "system", "content": "sys"}]
        out = apply_cache_markers(msgs, "explicit")
        assert out[0]["content"][-1].get("cache_control")

    def test_tool_message_is_markable(self):
        # A tool-result turn: the rolling breakpoint must be able to land on a
        # role:"tool" message (string content gets promoted to a text block).
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result text"},
            {"role": "assistant", "content": "final answer"},
        ]
        out = apply_cache_markers(msgs, "explicit")
        # tool (idx 2) and final assistant (idx 3) are the rolling pair.
        assert out[2]["content"][-1].get("cache_control")
        assert out[3]["content"][-1].get("cache_control")
        # assistant-with-tool-calls (idx 1) has content=None — not marked, not crashed.
        assert out[1].get("content") is None

    def test_empty_content_assistant_not_crashed(self):
        # Assistant message with content=None (tool-only turn). Must not raise.
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "user", "content": "next"},
        ]
        out = apply_cache_markers(msgs, "explicit")
        # Should not raise; the None-content assistant is skipped, user marked.
        assert out[2]["content"][-1].get("cache_control")


# ---------------------------------------------------------------------------
# Idempotency (feed replay safety)
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_already_marked_system_not_double_marked(self):
        msgs = [
            {"role": "system", "content": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": "hi"},
        ]
        out = apply_cache_markers(msgs, "explicit")
        # Still exactly one cache_control on the system block.
        sys_blocks = out[0]["content"]
        assert sum(1 for b in sys_blocks if isinstance(b, dict) and b.get("cache_control")) == 1

    def test_replay_produces_same_marker_count(self):
        # Simulate feed replay: messages already carry markers from a prior turn.
        msgs = [
            {"role": "system", "content": [
                {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [
                {"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}}]},
        ]
        out = apply_cache_markers(msgs, "explicit")
        total = _count_markers(out)
        assert total == 2  # not 4 — no duplication

    def test_double_apply_is_stable(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}]
        once = apply_cache_markers(msgs, "explicit")
        twice = apply_cache_markers(once, "explicit")
        assert _count_markers(once) == _count_markers(twice)


# ---------------------------------------------------------------------------
# Marker budget
# ---------------------------------------------------------------------------

class TestMarkerBudget:
    def test_total_markers_under_four(self):
        # Even with many messages, total cache_control markers stay ≤ 4.
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(20):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
        out = apply_cache_markers(msgs, "explicit")
        assert _count_markers(out) <= 4


# ---------------------------------------------------------------------------
# Helper-function unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_promote_string_to_blocks(self):
        out = _promote_to_blocks({"role": "system", "content": "hello"})
        assert out["content"] == [{"type": "text", "text": "hello"}]

    def test_promote_already_blocks_unchanged(self):
        msg = {"role": "system", "content": [{"type": "text", "text": "x"}]}
        assert _promote_to_blocks(msg) is msg or _promote_to_blocks(msg) == msg

    def test_promote_none_content_unchanged(self):
        msg = {"role": "assistant", "content": None}
        out = _promote_to_blocks(msg)
        assert out["content"] is None

    def test_mark_last_block_appends_marker(self):
        msg = {"role": "user", "content": "hi"}
        out = _mark_last_block(msg)
        assert out["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_mark_last_block_idempotent(self):
        msg = {"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}
        out = _mark_last_block(msg)
        assert out["content"][-1]["cache_control"] == {"type": "ephemeral"}
        # Only one marker, not two.
        assert sum(1 for b in out["content"] if b.get("cache_control")) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _has_marker(msg: dict) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("cache_control") for b in content)


def _count_markers(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("cache_control"):
                n += 1
    return n
