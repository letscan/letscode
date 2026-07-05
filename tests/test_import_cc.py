"""Tests for the Claude Code session importer.

Covers the format mapping (user/assistant/tool_use/tool_result), noise
filtering, compact-continuation handling, tool pairing, and end-to-end
validity of the produced feed via load_feed.
"""

import json
from pathlib import Path

import pytest

from letscode.importers.cc import convert_cc_session
from letscode.importers.report import render_report_md


# ---------------------------------------------------------------------------
# Helpers to build CC-style records
# ---------------------------------------------------------------------------


def _cc_line(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False)


def _write_cc_log(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(_cc_line(r) for r in records) + "\n", "utf-8")


def _first_record(**overrides) -> dict:
    base = {
        "type": "user",
        "cwd": "/tmp/project",
        "version": "2.1.153",
        "sessionId": "test-session",
        "timestamp": "2026-07-01T10:00:00.000Z",
    }
    base.update(overrides)
    return base


def _user_text(text: str) -> dict:
    return _first_record(message={"role": "user", "content": text})


def _user_tool_result(tool_use_id: str, content, is_error=False) -> dict:
    return _first_record(
        message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": content, "is_error": is_error}
        ]}
    )


def _assistant(*blocks: dict) -> dict:
    return _first_record(type="assistant", message={"role": "assistant", "content": list(blocks)})


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _thinking_block(text: str) -> dict:
    return {"type": "thinking", "thinking": text}


def _tool_use(id_: str, name: str, input_: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def _read_out(path: str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text("utf-8").strip().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Conversion correctness
# ---------------------------------------------------------------------------


class TestBasicMapping:
    def test_user_prompt_becomes_prompt_event(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [_user_text("hello world")])

        report = convert_cc_session(str(cc), str(out))

        events = _read_out(out)
        assert events[0]["type"] == "init"
        assert events[1] == {
            "type": "prompt", "timestamp": events[1]["timestamp"],
            "data": [{"type": "text", "text": "hello world"}],
        }
        assert report.user_prompts == 1
        assert report.converted_events == 2  # init + prompt

    def test_init_synthesized_from_first_record(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [_user_text("hi")])

        convert_cc_session(str(cc), str(out))

        init = _read_out(out)[0]
        assert init["type"] == "init"
        assert init["data"]["cwd"] == "/tmp/project"
        assert init["data"]["version"] == "2.1.153"
        assert init["data"]["model"] == "claude-unknown"
        assert init["data"]["agent"] == "claude-code (imported)"

    def test_assistant_text_becomes_agent_message_chunk(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("hi"),
            _assistant(_text_block("hello there")),
        ])

        convert_cc_session(str(cc), str(out))

        events = _read_out(out)
        chunk = next(e for e in events if e["type"] == "agent_message_chunk")
        assert chunk["data"] == {"type": "text", "text": "hello there"}


class TestToolPairing:
    def test_tool_use_then_result_pairs_correctly(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("read foo.py"),
            _assistant(_tool_use("call_1", "Read", {"file_path": "foo.py"})),
            _user_tool_result("call_1", "contents of foo.py"),
        ])

        report = convert_cc_session(str(cc), str(out))
        events = _read_out(out)

        tool_call = next(e for e in events if e["type"] == "tool_call")
        assert tool_call["data"]["toolCallId"] == "call_1"
        assert tool_call["data"]["toolName"] == "Read"
        assert tool_call["data"]["rawInput"] == {"file_path": "foo.py"}

        update = next(e for e in events if e["type"] == "tool_call_update")
        assert update["data"]["toolCallId"] == "call_1"
        assert update["data"]["status"] == "completed"
        assert update["data"]["rawOutput"] == "contents of foo.py"
        assert report.tool_use_blocks == 1
        assert report.tool_results == 1

    def test_tool_result_is_error_becomes_failed(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("read missing"),
            _assistant(_tool_use("c1", "Read", {"file_path": "x"})),
            _user_tool_result("c1", "File not found", is_error=True),
        ])

        report = convert_cc_session(str(cc), str(out))
        update = next(e for e in _read_out(out) if e["type"] == "tool_call_update")

        assert update["data"]["status"] == "failed"
        assert update["data"]["rawOutput"] == "<error>File not found</error>"
        assert report.is_error_results == 1

    def test_tool_result_list_content_flattened(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        list_content = [{"type": "text", "text": "line one"},
                        {"type": "text", "text": "line two"}]
        _write_cc_log(cc, [
            _user_text("run"),
            _assistant(_tool_use("c1", "Bash", {"command": "echo"})),
            _user_tool_result("c1", list_content),
        ])

        report = convert_cc_session(str(cc), str(out))
        update = next(e for e in _read_out(out) if e["type"] == "tool_call_update")

        assert update["data"]["rawOutput"] == "line one\nline two"
        assert report.tool_result_list_content == 1
        assert report.tool_result_string_content == 0

    def test_orphan_tool_result_skipped(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_tool_result("nonexistent_id", "ghost result"),
        ])

        report = convert_cc_session(str(cc), str(out))

        assert report.orphan_tool_results == 1
        assert not any(e["type"] == "tool_call_update" for e in _read_out(out))


class TestNoiseFiltering:
    def test_slash_command_markers_dropped(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("<command-name>/clear</command-name>\n<command-message>clear</command-message>"),
            _user_text("<local-command-stdout>cleared</local-command-stdout>"),
            _user_text("real prompt"),
        ])

        report = convert_cc_session(str(cc), str(out))
        events = _read_out(out)

        prompts = [e for e in events if e["type"] == "prompt"]
        assert len(prompts) == 1
        assert prompts[0]["data"][0]["text"] == "real prompt"
        assert report.skipped_user_markers == 2

    def test_thinking_blocks_dropped(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("hi"),
            _assistant(_thinking_block("internal reasoning"), _text_block("answer")),
        ])

        report = convert_cc_session(str(cc), str(out))

        assert report.thinking_blocks_dropped == 1
        assert report.agent_text_blocks == 1

    def test_system_attachment_types_skipped(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("hi"),
            {"type": "system", "subtype": "turn_duration", "content": "5s"},
            {"type": "attachment", "attachment": {"type": "skill_listing"}},
            {"type": "ai-title", "title": "My Session"},
        ])

        report = convert_cc_session(str(cc), str(out))

        assert report.skipped_types["system"] == 1
        assert report.skipped_types["attachment"] == 1
        assert report.skipped_types["ai-title"] == 1

    def test_sidechain_entries_dropped(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("hi"),
            _first_record(type="assistant", isSidechain="True",
                          message={"role": "assistant", "content": [_text_block("subagent")]}),
        ])

        report = convert_cc_session(str(cc), str(out))

        assert report.sidechain_entries == 1
        assert report.agent_text_blocks == 0  # the sidechain text was dropped


class TestCompactContinuation:
    def test_compact_summary_becomes_user_message_chunk(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        summary = ("This session is being continued from a previous conversation "
                   "that ran out of context. The summary below covers the earlier portion.")
        _write_cc_log(cc, [
            _user_text(summary),
            _user_text("continue working"),
        ])

        report = convert_cc_session(str(cc), str(out))
        events = _read_out(out)

        chunks = [e for e in events if e["type"] == "user_message_chunk"]
        prompts = [e for e in events if e["type"] == "prompt"]
        assert len(chunks) == 1
        assert chunks[0]["data"]["text"].startswith("This session is being continued")
        assert len(prompts) == 1
        assert report.compact_continuations == 1
        assert report.user_prompts == 1


class TestReport:
    def test_report_renders_markdown_with_all_sections(self, tmp_path):
        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("hi"),
            _assistant(_thinking_block("think"), _tool_use("c1", "Agent", {"prompt": "x"})),
            _user_tool_result("c1", [{"type": "text", "text": "subagent answer"}]),
            {"type": "system", "content": "noise"},
        ])

        report = convert_cc_session(str(cc), str(out))
        md = render_report_md(report)

        assert "Claude Code → letscode 导入分析报告" in md
        assert "已映射的 CC 特性" in md
        assert "letscode 暂不支持的 CC 特性" in md
        assert "thinking blocks" in md
        assert "Agent subagent" in md
        # The report reflects real counts
        assert "1" in md  # at least one tool_use / thinking / etc.


# ---------------------------------------------------------------------------
# End-to-end: produced feed is a valid letscode session
# ---------------------------------------------------------------------------


class TestEndToEndReplay:
    def test_load_feed_reconstructs_legal_messages(self, tmp_path):
        """The imported feed must replay into a valid OpenAI messages list."""
        from letscode.feed import load_feed

        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("read foo.py and tell me what it does"),
            _assistant(
                _text_block("Let me read it."),
                _tool_use("c1", "Read", {"file_path": "foo.py"}),
            ),
            _user_tool_result("c1", "def foo(): pass"),
            _assistant(_text_block("It defines an empty function.")),
        ])

        convert_cc_session(str(cc), str(out))
        model, messages = load_feed(str(out))

        assert model == "claude-unknown"
        roles = [m["role"] for m in messages]
        # user, assistant(tool), tool, assistant
        assert roles == ["user", "assistant", "tool", "assistant"], roles
        # tool_call_id pairing is consistent
        assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"]

    def test_compact_then_prompt_is_consecutive_user(self, tmp_path):
        """Imported compact continuation + real prompt replay as consecutive user."""
        from letscode.feed import load_feed

        cc = tmp_path / "cc.jsonl"
        out = tmp_path / "out.jsonl"
        _write_cc_log(cc, [
            _user_text("This session is being continued from a previous conversation."),
            _user_text("keep going"),
            _assistant(_text_block("ok")),
        ])

        convert_cc_session(str(cc), str(out))
        _, messages = load_feed(str(out))

        roles = [m["role"] for m in messages]
        assert roles == ["user", "user", "assistant"], roles
