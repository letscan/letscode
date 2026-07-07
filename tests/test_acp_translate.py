"""Tests for ACP event translation (`_translate_event`).

Focuses on the tool_call / tool_call_update translation, in particular the
in_progress placeholder-content behavior that makes a tool card expandable
in Zed while the tool is still running.
"""

import pytest

from letscode.acp.server import _translate_event


def _tool_call(tc_id="tc1", name="Bash", raw_input=None):
    return {
        "type": "tool_call",
        "data": {
            "toolCallId": tc_id,
            "toolName": name,
            "rawInput": raw_input if raw_input is not None else {"command": "ls"},
        },
    }


def _tool_call_update(tc_id="tc1", status=None, raw_output=None):
    data = {"toolCallId": tc_id}
    if status is not None:
        data["status"] = status
    if raw_output is not None:
        data["rawOutput"] = raw_output
    return {"type": "tool_call_update", "data": data}


class TestInProgressPlaceholder:
    """An in_progress update must carry placeholder content so the ACP client
    (Zed) shows the expand affordance while the tool runs — otherwise the card
    stays collapsed and raw_input is unreachable."""

    def test_in_progress_emits_placeholder_content(self):
        pending: dict = {}
        # tool_call first (populates the pending cache with name/input)
        start = _translate_event(_tool_call(), pending)
        assert start.session_update == "tool_call"
        assert pending["tc1"] == {"input": {"command": "ls"}, "name": "Bash"}

        upd = _translate_event(_tool_call_update(status="in_progress"), pending)
        assert upd.session_update == "tool_call_update"
        assert upd.status == "in_progress"
        # Placeholder content present (non-empty list with a text block)
        assert upd.content is not None
        assert len(upd.content) == 1
        # The block carries the ellipsis placeholder text
        assert _extract_text(upd.content) == "…"

    def test_placeholder_replaced_by_completed_result(self):
        """The placeholder occupies content slot 0 and must be positionally
        replaced by the real result on completion (no lingering placeholder)."""
        pending: dict = {}
        _translate_event(_tool_call(), pending)
        in_prog = _translate_event(_tool_call_update(status="in_progress"), pending)
        assert _extract_text(in_prog.content) == "…"

        completed = _translate_event(
            _tool_call_update(status="completed", raw_output="hello world"),
            pending,
        )
        # Completed content is the real Bash output, not the placeholder.
        assert completed.status == "completed"
        assert _extract_text(completed.content) != "…"
        assert "hello world" in _extract_text(completed.content)
        # pending cache is cleared on terminal status
        assert "tc1" not in pending

    def test_placeholder_replaced_by_diff_for_edit(self):
        """Edit completion sends a diff content block; the placeholder (a text
        block) is replaced wholesale via update_from_acp's type-mismatch path."""
        pending: dict = {}
        _translate_event(
            _tool_call(
                name="Edit",
                raw_input={"file_path": "/tmp/f.txt",
                           "old_string": "a", "new_string": "b"},
            ),
            pending,
        )
        _translate_event(_tool_call_update(status="in_progress"), pending)
        completed = _translate_event(_tool_call_update(status="completed"), pending)
        assert completed.status == "completed"
        # Edit completion with a file_path builds a diff (type='diff'), not a
        # text block — so it is NOT the placeholder ellipsis.
        assert completed.content is not None
        assert len(completed.content) == 1
        assert completed.content[0].type == "diff"


class TestNoRegression:
    """Streaming output chunks (no status) and completed events without a prior
    tool_call must continue to behave as before."""

    def test_streaming_chunk_without_status_carries_no_placeholder(self):
        pending: dict = {}
        _translate_event(_tool_call(), pending)
        upd = _translate_event(_tool_call_update(raw_output="partial"), pending)
        # A status-less streaming chunk must NOT inject placeholder content —
        # that would be spurious and would grow the content list on every chunk.
        assert upd.content is None

    def test_pending_status_does_not_inject_placeholder(self):
        """Only in_progress triggers the placeholder. The initial start event
        already sets status=pending; a redundant pending update must not inject
        a placeholder (it would double the content slot)."""
        pending: dict = {}
        _translate_event(_tool_call(), pending)
        upd = _translate_event(_tool_call_update(status="pending"), pending)
        assert upd.content is None


class TestFallbackContentForTextTools:
    """Tools without a dedicated _build_completed_content branch (Glob, Grep,
    Skill, Agent, MCP tools) must still produce content on completion from
    rawOutput, replacing the in_progress placeholder. Otherwise the placeholder
    ellipsis lingers in the card's Output slot forever."""

    @pytest.mark.parametrize("name,raw_input", [
        ("Glob", {"pattern": "*.py"}),
        ("Grep", {"pattern": "foo"}),
        ("Skill", {"skill": "docx"}),
        ("Agent", {"prompt": "do something"}),
    ])
    def test_text_tools_replace_placeholder_on_completion(self, name, raw_input):
        pending: dict = {}
        _translate_event(_tool_call(name=name, raw_input=raw_input), pending)
        in_prog = _translate_event(_tool_call_update(status="in_progress"), pending)
        assert _extract_text(in_prog.content) == "…"

        output = "sample tool output"
        completed = _translate_event(
            _tool_call_update(status="completed", raw_output=output),
            pending,
        )
        assert completed.status == "completed"
        # Placeholder is replaced by the real rawOutput, not lingering.
        assert _extract_text(completed.content) == output

    def test_empty_raw_output_falls_through_to_none(self):
        """If a fallback tool produces no output, no content is synthesized
        (nothing to show; the placeholder staying is acceptable since there's
        genuinely no result text)."""
        pending: dict = {}
        _translate_event(_tool_call(name="Glob", raw_input={"pattern": "x"}), pending)
        _translate_event(_tool_call_update(status="in_progress"), pending)
        completed = _translate_event(
            _tool_call_update(status="completed", raw_output=""),
            pending,
        )
        assert completed.content is None


def _extract_text(content) -> str:
    """Flatten an ACP tool content list to a comparable string.

    `h.tool_content(h.text_block(...))` produces a ContentToolCallContent whose
    `.content` is a TextContentBlock with a `.text` field. Diff/other variants
    fall back to a stringified repr so the caller can still distinguish them.
    """
    if not content:
        return ""
    parts = []
    for item in content:
        inner = getattr(item, "content", None)
        text = getattr(inner, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(str(item))
    return "".join(parts)
