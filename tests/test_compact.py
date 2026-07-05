"""Tests for /compact: summary as user role + feed file rotation.

Covers:
- MessageSubscriber._add_extra_user_message no longer drops standalone
  user messages (the drop bug); skill-expansion path is unchanged.
- /compact writes a flat user_message_chunk, backs up the old log, and
  produces a new log whose replay yields a consecutive-user sequence.
- _translate_event forwards user_message_chunk to the ACP client.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from letscode.subscribers import MessageSubscriber


# ---------------------------------------------------------------------------
# MessageSubscriber._add_extra_user_message — drop-bug fix
# ---------------------------------------------------------------------------


class TestStandaloneUserMessage:
    """A user_message_chunk with no pending tool must be retained, not dropped."""

    def test_standalone_user_message_is_appended(self):
        sub = MessageSubscriber()
        sub("user_message_chunk", {"type": "text", "text": "standalone summary"})
        sub.flush()
        assert sub.messages == [{"role": "user", "content": "standalone summary"}]

    def test_empty_text_is_still_dropped(self):
        sub = MessageSubscriber()
        sub("user_message_chunk", {"type": "text", "text": ""})
        sub.flush()
        assert sub.messages == []

    def test_compact_summary_then_prompt_is_consecutive_user(self):
        """The core compact scenario: summary(user) → real prompt(user) → assistant."""
        sub = MessageSubscriber()
        sub("user_message_chunk", {"type": "text", "text": "[来自 compact 的上下文摘要]\n..."})
        sub("prompt", [{"type": "text", "text": "继续"}])
        sub("agent_message_chunk", {"type": "text", "text": "好的"})
        sub.flush()
        roles = [m["role"] for m in sub.messages]
        assert roles == ["user", "user", "assistant"], roles

    def test_orphan_after_flush_is_retained(self):
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "task A"}])
        sub("agent_message_chunk", {"type": "text", "text": "doing A"})
        sub.flush()
        sub("user_message_chunk", {"type": "text", "text": "late skill content"})
        sub.flush()
        roles = [m["role"] for m in sub.messages]
        assert roles == ["user", "assistant", "user"], roles


class TestSkillExpansionUnchanged:
    """When a tool is pending, the user message attaches after the tool result."""

    def test_user_message_attaches_after_tool_result(self):
        sub = MessageSubscriber()
        sub("prompt", [{"type": "text", "text": "set up git"}])
        sub("agent_message_chunk", {"type": "text", "text": "Loading skill."})
        sub("tool_call", {"toolCallId": "t1", "toolName": "Skill", "rawInput": {"name": "git"}})
        sub("tool_call_update", {"toolCallId": "t1", "status": "in_progress"})
        sub("user_message_chunk", {"type": "text", "text": "[Skill: git]\n<skill body>"})
        sub("tool_call_update", {"toolCallId": "t1", "status": "completed", "rawOutput": "Loaded"})
        sub.flush()
        roles = [m["role"] for m in sub.messages]
        # user, assistant(tool), tool, user(skill) — skill attaches after tool result
        assert roles == ["user", "assistant", "tool", "user"], roles
        assert sub.messages[3]["content"] == "[Skill: git]\n<skill body>"


# ---------------------------------------------------------------------------
# /compact handler — output format + file rotation
# ---------------------------------------------------------------------------


def _make_log(path: Path, turns: list[list[dict]]) -> None:
    """Write a feed log: one init, then the given turns of events."""
    events = [{"type": "init", "data": {"model": "test-model", "cwd": "/tmp"}}]
    for turn in turns:
        events.extend(turn)
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", "utf-8")


def _turn(prompt: str, answer: str) -> list[dict]:
    return [
        {"type": "prompt", "data": [{"type": "text", "text": prompt}]},
        {"type": "agent_message_chunk", "data": {"type": "text", "text": answer}},
        {"type": "result", "data": {"stopReason": "end_turn", "turns": 1, "toolCalls": 0}},
    ]


def _turn_with_tool(prompt: str, answer: str, tid: str, tool_name: str,
                    tool_input: dict, result_text: str) -> list[dict]:
    """A turn whose assistant reply includes a tool call + result."""
    return [
        {"type": "prompt", "data": [{"type": "text", "text": prompt}]},
        {"type": "agent_message_chunk", "data": {"type": "text", "text": answer}},
        {"type": "tool_call", "data": {"toolCallId": tid, "toolName": tool_name, "rawInput": tool_input}},
        {"type": "tool_call_update", "data": {"toolCallId": tid, "status": "in_progress"}},
        {"type": "tool_call_update", "data": {"toolCallId": tid, "status": "completed", "rawOutput": result_text}},
        {"type": "result", "data": {"stopReason": "end_turn", "turns": 1, "toolCalls": 1}},
    ]


class _FakeSession:
    def __init__(self, log_path: str):
        self.log_path = log_path


class TestHandleCompactOutput:
    """_handle_compact produces a flat user_message_chunk and rotates the log."""

    def test_summary_written_as_user_message_chunk(self, tmp_path):
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary text"):
            result = _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        # Find the summary event
        summaries = [e for e in events if e["type"] == "user_message_chunk"]
        assert len(summaries) == 1
        assert summaries[0]["data"]["text"].startswith("[来自 compact 的上下文摘要]")
        assert "summary text" in summaries[0]["data"]["text"]
        assert result.message.startswith("已压缩上下文")

    def test_new_log_contains_init(self, tmp_path):
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        inits = [e for e in events if e["type"] == "init"]
        assert len(inits) == 1, "new log must have exactly one init"
        assert inits[0]["data"]["model"] == "test-model"

    def test_init_not_duplicated_from_kept_turn(self, tmp_path):
        """The kept turn (via split_turns merge) carries init; it must not duplicate."""
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        init_count = sum(1 for e in events if e["type"] == "init")
        assert init_count == 1

    def test_old_log_backed_up(self, tmp_path):
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        backup = tmp_path / "session.jsonl.compact1.bak"
        assert backup.exists(), "original log must be backed up"

    def test_repeated_compact_increments_backup_suffix(self, tmp_path):
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2"), _turn("p3", "a3")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary v1"):
            _handle_compact(_FakeSession(str(log)), config=None)
        assert (tmp_path / "session.jsonl.compact1.bak").exists()

        # Add another turn to allow a second compact
        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        events.extend(_turn("p4", "a4"))
        log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", "utf-8")

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary v2"):
            _handle_compact(_FakeSession(str(log)), config=None)
        assert (tmp_path / "session.jsonl.compact2.bak").exists()

    def test_fallback_when_llm_fails(self, tmp_path):
        """When LLM summarization returns None, no summary event is written."""
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value=None):
            result = _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        assert not any(e["type"] == "user_message_chunk" for e in events)
        assert (tmp_path / "session.jsonl.compact1.bak").exists()
        assert "已压缩" in result.message


class TestCompactReplayEndToEnd:
    """A compacted log replays as user(summary) → user(prompt) → assistant."""

    def test_load_feed_reconstructs_consecutive_user(self, tmp_path):
        from letscode.feed import load_feed
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2"), _turn("continue work", "ok")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="之前的进展"):
            _handle_compact(_FakeSession(str(log)), config=None)

        model, messages = load_feed(str(log))
        assert model == "test-model"
        roles = [m["role"] for m in messages]
        # summary(user) leads, then kept turns. With 3 input turns and
        # keep_count=min(3,2)=2, the last 2 turns are kept: p2/a2 + continue/ok.
        # summary + p2 + a2 + continue + ok.
        assert roles[0] == "user", "summary must lead as user"
        assert roles[:2] == ["user", "user"], "summary + first kept prompt are consecutive user"
        assert "之前的进展" in messages[0]["content"]


class TestKeepRecentTurns:
    """The most recent 3 turns are kept (clamped when fewer turns exist)."""

    def test_keeps_last_3_turns_when_5_input(self, tmp_path):
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn(f"p{i}", f"a{i}") for i in range(5)])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        prompts = [e for e in events if e["type"] == "prompt"]
        # init + summary + prompts p2,p3,p4 (last 3 turns kept)
        assert len(prompts) == 3
        assert prompts[0]["data"][0]["text"] == "p2"
        assert prompts[-1]["data"][0]["text"] == "p4"

    def test_keep_count_clamps_when_few_turns(self, tmp_path):
        """2 input turns → keep min(3, 1) = 1 turn."""
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [_turn("p1", "a1"), _turn("p2", "a2")])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        prompts = [e for e in events if e["type"] == "prompt"]
        assert len(prompts) == 1
        assert prompts[0]["data"][0]["text"] == "p2"


class TestToolResultStubbing:
    """Tool results in kept turns are stubbed; tool-call structure is preserved."""

    def test_tool_result_replaced_with_stub(self, tmp_path):
        from letscode.acp.commands import _handle_compact, _TOOL_RESULT_STUB

        log = tmp_path / "session.jsonl"
        big_result = "X" * 5000
        _make_log(log, [
            _turn("earlier", "old"),
            _turn_with_tool("read foo", "looking", "t1", "Read",
                            {"file_path": "foo.py"}, big_result),
        ])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        updates = [e for e in events if e["type"] == "tool_call_update"
                   and e["data"].get("status") == "completed"]
        assert len(updates) == 1
        assert updates[0]["data"]["rawOutput"] == _TOOL_RESULT_STUB
        # The big result must NOT appear in the compacted log
        assert big_result not in log.read_text("utf-8")

    def test_tool_call_event_kept_verbatim(self, tmp_path):
        """The tool_call event (which tool, what args) survives compaction."""
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [
            _turn("earlier", "old"),
            _turn_with_tool("read", "looking", "t1", "Read",
                            {"file_path": "foo.py"}, "file contents"),
        ])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["data"]["toolName"] == "Read"
        assert tool_calls[0]["data"]["rawInput"] == {"file_path": "foo.py"}

    def test_stubbed_feed_replays_with_valid_tool_pairing(self, tmp_path):
        """Stubbed results still produce a legal assistant(tool_calls) → tool sequence."""
        from letscode.feed import load_feed
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [
            _turn("earlier", "old"),
            _turn_with_tool("read", "looking", "t1", "Bash",
                            {"command": "ls"}, "file1\nfile2\nfile3"),
        ])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        _, messages = load_feed(str(log))
        roles = [m["role"] for m in messages]
        # summary(user), user(read), assistant(looking+tool_call), tool(stub)
        assert roles == ["user", "user", "assistant", "tool"], roles
        # tool_call_id pairing intact
        tc_id = messages[2]["tool_calls"][0]["id"]
        assert messages[3]["tool_call_id"] == tc_id
        assert messages[3]["content"] == "<tool result omitted>"

    def test_in_progress_update_not_affected(self, tmp_path):
        """Only completed/failed tool_call_update gets stubbed; in_progress is status-only already."""
        from letscode.acp.commands import _handle_compact

        log = tmp_path / "session.jsonl"
        _make_log(log, [
            _turn("earlier", "old"),
            _turn_with_tool("read", "looking", "t1", "Read",
                            {"file_path": "foo.py"}, "contents"),
        ])

        with patch("letscode.acp.commands._try_llm_summarize", return_value="summary"):
            _handle_compact(_FakeSession(str(log)), config=None)

        events = [json.loads(l) for l in log.read_text("utf-8").strip().splitlines()]
        in_progress = [e for e in events if e["type"] == "tool_call_update"
                       and e["data"].get("status") == "in_progress"]
        assert len(in_progress) == 1
        assert "rawOutput" not in in_progress[0]["data"]


# ---------------------------------------------------------------------------
# _translate_event — user_message_chunk forwarded to ACP client
# ---------------------------------------------------------------------------


class TestTranslateUserMessageChunk:
    def test_user_message_chunk_translated_to_update_user_message(self):
        from letscode.acp.server import _translate_event

        ev = {"type": "user_message_chunk", "data": {"type": "text", "text": "injected content"}}
        upd = _translate_event(ev, {})
        assert upd is not None
        assert upd.session_update == "user_message_chunk"
        assert upd.content.text == "injected content"

    def test_empty_user_message_chunk_returns_none(self):
        from letscode.acp.server import _translate_event

        ev = {"type": "user_message_chunk", "data": {"type": "text", "text": ""}}
        upd = _translate_event(ev, {})
        assert upd is None
