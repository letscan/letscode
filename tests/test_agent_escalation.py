"""Tests for the passive permission-escalation trigger in agent.py.

After the best-effort loop ends naturally, agent.py inspects the ToolRunner's
collected denials and emits an ``error`` event with ``code="permission_denied"``
when either (a) ≥2 denials accumulated or (b) the last tool call was itself a
denial. The agent loop itself is unchanged — no RequestPermission tool is
exposed; the trigger is a pure post-hoc harness decision.

These tests stub consume_stream_async (so no LLM is called) and use a fake
ToolRunner to feed denial records directly.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from letscode.agent import run_agent
from letscode.config import ModelConfig
from letscode.events import EventHub, set_hub
from letscode.stream import StreamResult
from letscode.tools._types import ToolResult
from letscode.tools.runner import ToolRunner


def _model_config() -> ModelConfig:
    return ModelConfig(
        model="m1", api_key="k", base_url="http://x",
        max_tokens=1000, cache="auto", preset="default", sandbox=False,
    )


class _FakeRunner:
    """Stand-in for ToolRunner that yields a fixed result and exposes a
    controllable denials list + last_call_denied flag.

    agent.py reads ``runner.denials`` and sets ``runner._last_call_denied``
    after each dispatch. We mimic the real ToolRunner's interface closely
    enough for run_agent to operate on it.
    """

    def __init__(self, result: ToolResult, denials: list[dict]):
        self._result = result
        self._denials = denials
        self._last_call_denied = False

    @property
    def definitions(self):
        return []

    @property
    def rules(self):
        from letscode.rules import Rules
        return Rules()

    @property
    def denials(self):
        return self._denials

    @property
    def last_call_denied(self):
        return self._last_call_denied

    async def execute(self, name, arguments):
        yield self._result


def _stream_with_tool_call(tool_name="Bash", tool_id="tc1", args="{}"):
    """A StreamResult that requests one tool call."""
    from letscode.stream import ToolCall
    return StreamResult(
        text_content="",
        tool_calls=[ToolCall(id=tool_id, name=tool_name, arguments=args)],
    )


def _stream_text_only(text="done"):
    """A StreamResult that ends the loop (no tool calls)."""
    return StreamResult(text_content=text, tool_calls=[])


def _run_agent_with_streams(
    streams: list, runner: _FakeRunner, hub: EventHub,
):
    """Drive run_agent through a sequence of canned StreamResults.

    The first stream is the initial prompt's response; subsequent ones follow
    tool executions. The loop ends when a stream has no tool_calls.
    """
    iterator = iter(streams)

    async def fake_consume(*a, **kw):
        try:
            return next(iterator)
        except StopIteration:
            return _stream_text_only()

    async def go():
        with patch("letscode.agent.consume_stream_async", new=AsyncMock(
            side_effect=fake_consume,
        )):
            return await run_agent(
                prompt_blocks=[{"type": "text", "text": "do it"}],
                system_prompt="sys",
                config=_model_config(),
                tool_runner=runner,  # type: ignore[arg-type]
                msg_sub=None,
            )
    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Trigger: ≥2 denials → emit permission_denied
# ---------------------------------------------------------------------------

class TestTriggerTwoDenials:
    def test_two_denials_emits_error(self):
        errors: list[dict] = []
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(lambda t, d: errors.append({"type": t, "data": d}) if t == "error" else None)

        runner = _FakeRunner(
            ToolResult(content="<error>denied</error>", success=False),
            denials=[
                {"type": "write", "target": "/a"},
                {"type": "write", "target": "/b"},
            ],
        )
        # 1st stream: tool call (denied) → 2nd stream: text (loop ends)
        _run_agent_with_streams(
            [_stream_with_tool_call(), _stream_text_only()], runner, hub,
        )
        perm_errors = [e for e in errors if e["data"].get("code") == "permission_denied"]
        assert len(perm_errors) == 1
        data = perm_errors[0]["data"]
        assert data["recoverable"] is True
        assert len(data["denials"]) == 2
        assert data["denials"][0] == {"type": "write", "target": "/a"}

    def test_single_denial_not_last_call_no_error(self):
        """One denial followed by a successful call (last_call_denied=False)
        and no further denials → no trigger."""
        errors: list = []
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(lambda t, d: errors.append(d) if t == "error" else None)

        runner = _FakeRunner(
            ToolResult(content="ok", success=True),
            denials=[{"type": "write", "target": "/a"}],  # only 1 denial
        )
        _run_agent_with_streams(
            [_stream_with_tool_call(), _stream_text_only()], runner, hub,
        )
        assert not any(d.get("code") == "permission_denied" for d in errors)


# ---------------------------------------------------------------------------
# Trigger: last call denied (even with just 1 denial total)
# ---------------------------------------------------------------------------

class TestTriggerLastCallDenied:
    def test_last_call_denied_emits_error(self):
        """Single denial on the LAST tool call: the "blocked at the finish
        line" trigger fires even though count < 2."""
        errors: list = []
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(lambda t, d: errors.append(d) if t == "error" else None)

        # Runner yields a denied result; the agent loop will mark
        # _last_call_denied=True (denial list grows by 1 during this call).
        # We pre-populate denials=[] and let the runner's execute NOT add to
        # it — but run_agent compares len before/after, so we need the
        # runner to actually append on dispatch. Use a subclass.
        class _GrowingRunner(_FakeRunner):
            async def execute(self, name, arguments):
                self._denials.append({"type": "write", "target": "/a"})
                yield ToolResult(content="<error>denied</error>", success=False)

        runner = _GrowingRunner(
            ToolResult(content="<error>denied</error>", success=False),
            denials=[],
        )
        # 1st stream: tool call → 2nd stream: text ends loop
        _run_agent_with_streams(
            [_stream_with_tool_call(), _stream_text_only()], runner, hub,
        )
        perm_errors = [d for d in errors if d.get("code") == "permission_denied"]
        assert len(perm_errors) == 1
        assert perm_errors[0]["denials"] == [{"type": "write", "target": "/a"}]


# ---------------------------------------------------------------------------
# No trigger when no denials
# ---------------------------------------------------------------------------

class TestNoTrigger:
    def test_no_denials_no_error(self):
        errors: list = []
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(lambda t, d: errors.append(d) if t == "error" else None)

        runner = _FakeRunner(
            ToolResult(content="ok", success=True),
            denials=[],
        )
        _run_agent_with_streams(
            [_stream_with_tool_call(), _stream_text_only()], runner, hub,
        )
        assert not any(d.get("code") == "permission_denied" for d in errors)

    def test_text_only_response_no_error(self):
        """No tool calls at all → no denials → no trigger."""
        errors: list = []
        hub = EventHub()
        set_hub(hub)
        hub.subscribe(lambda t, d: errors.append(d) if t == "error" else None)

        runner = _FakeRunner(
            ToolResult(content="ok", success=True), denials=[],
        )
        _run_agent_with_streams([_stream_text_only("hello")], runner, hub)
        assert not any(d.get("code") == "permission_denied" for d in errors)
