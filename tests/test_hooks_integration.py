"""Integration tests for onAgentStart/onAgentEnd hooks in the real agent loop.

These tests mock the LLM call (consume_stream_async) to return scripted
tool_calls, then verify that the hooks fire at the right lifecycle points and
their output is injected correctly. No API key needed.
"""

import asyncio
import json
import os
import stat
from collections import namedtuple
from unittest.mock import patch

import pytest

from letscode.agent import run_agent
from letscode.config import ModelConfig
from letscode.subscribers import MessageSubscriber
from letscode.tools._types import ToolResult

StreamResult = namedtuple("StreamResult", ["text_content", "tool_calls", "usage"])
ToolCall = namedtuple("ToolCall", ["id", "name", "arguments"])


def _make_config():
    return ModelConfig(
        model="mock", api_key="dummy", base_url="http://localhost",
        max_tokens=100, preset="default", sandbox=False,
    )


class _ScriptedRunner:
    definitions = [{"function": {"name": "Bash", "description": "test", "parameters": {"type": "object", "properties": {}}}}]
    rules = type("R", (), {"allow_read": [], "deny_read": [], "allow_write": [], "deny_write": [], "allow_cmd": [], "deny_cmd": []})()
    denials = []
    _last_call_denied = False

    async def execute(self, name, args_json):
        yield ToolResult(content="ok", success=True)


def _make_executable(tmp_path, name, content):
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + content)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run_in_dir(tmp_path, coro):
    """Run a coroutine with cwd set to tmp_path, then restore."""
    old_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        return asyncio.run(coro)
    finally:
        os.chdir(old_cwd)


async def _run_agent(llm_turns, *, on_agent_start=None, on_agent_end=None, msg_sub=None, config_path=None):
    config = _make_config()
    runner = _ScriptedRunner()
    msg_sub = msg_sub or MessageSubscriber()
    call_idx = [0]

    async def _mock_consume_stream(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx >= len(llm_turns):
            return StreamResult(text_content="(done)", tool_calls=[], usage=None)
        return llm_turns[idx]

    with patch("letscode.agent.consume_stream_async", side_effect=_mock_consume_stream):
        with patch("letscode.agent.apply_cache_markers", side_effect=lambda msgs, cache: msgs):
            rc = await run_agent(
                prompt_blocks=[{"type": "text", "text": "test"}],
                system_prompt="test", config=config, max_turns=10,
                tool_runner=runner, msg_sub=msg_sub,
                on_agent_start=on_agent_start,
                on_agent_end=on_agent_end,
                config_path=config_path,
            )
    return rc, call_idx[0], msg_sub


class TestOnAgentStart:
    def test_stdout_injected_as_context(self, tmp_path):
        hook = _make_executable(tmp_path, "pre.sh", 'echo "PREPARE_PHASE=plan"')
        turn1 = StreamResult(text_content="", tool_calls=[ToolCall("t1", "Bash", "{}")], usage=None)
        turn2 = StreamResult(text_content="done", tool_calls=[], usage=None)

        rc, _, msg_sub = _run_in_dir(tmp_path, _run_agent(
            [turn1, turn2], on_agent_start=hook,
        ))
        messages_text = json.dumps(msg_sub.messages, ensure_ascii=False)
        assert "PREPARE_PHASE=plan" in messages_text

    def test_no_hook_means_no_injection(self, tmp_path):
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        rc, _, msg_sub = _run_in_dir(tmp_path, _run_agent([turn1]))
        assert "onAgentStart" not in json.dumps(msg_sub.messages, ensure_ascii=False)

    def test_nonexistent_script_skipped(self, tmp_path):
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        rc, _, _ = _run_in_dir(tmp_path, _run_agent(
            [turn1], on_agent_start="./nonexistent.sh",
        ))
        assert rc == 0

    def test_receives_empty_run_json(self, tmp_path):
        out = tmp_path / "stdin.json"
        hook = _make_executable(tmp_path, "pre.sh", f'cat > {out}')
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        _run_in_dir(tmp_path, _run_agent([turn1], on_agent_start=hook))
        data = json.loads(out.read_text())
        assert data["turn"] == 0
        assert data["tool_calls"] == []

    def test_abort_skips_agent_loop(self, tmp_path):
        """onAgentStart exit code 2 → agent loop is skipped, rc=1."""
        hook = _make_executable(tmp_path, "pre.sh",
            'echo "build failed, skipping"; exit 2')
        # These turns should never be consumed (loop doesn't run).
        turn1 = StreamResult(text_content="should not reach", tool_calls=[], usage=None)
        rc, llm_calls, _ = _run_in_dir(tmp_path, _run_agent(
            [turn1], on_agent_start=hook,
        ))
        assert rc == 1
        assert llm_calls == 0, "Agent loop should have been skipped entirely"


class TestOnAgentEnd:
    def test_runs_after_loop(self, tmp_path):
        marker = tmp_path / "ran.txt"
        hook = _make_executable(tmp_path, "post.sh", f'touch {marker}')
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        _run_in_dir(tmp_path, _run_agent([turn1], on_agent_end=hook))
        assert marker.exists()

    def test_receives_full_run_json(self, tmp_path):
        out = tmp_path / "stdin.json"
        hook = _make_executable(tmp_path, "post.sh", f'cat > {out}')
        turn1 = StreamResult(text_content="",
            tool_calls=[ToolCall("t1", "Bash", "{}")], usage=None)
        turn2 = StreamResult(text_content="done", tool_calls=[], usage=None)
        _run_in_dir(tmp_path, _run_agent([turn1, turn2], on_agent_end=hook))
        data = json.loads(out.read_text())
        assert data["turn"] >= 1
        assert len(data["tool_calls"]) >= 1

    def test_nonzero_exit_bumps_returncode(self, tmp_path):
        hook = _make_executable(tmp_path, "post.sh", 'exit 1')
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        rc, _, _ = _run_in_dir(tmp_path, _run_agent([turn1], on_agent_end=hook))
        assert rc == 1

    def test_abort_sets_error(self, tmp_path):
        """onAgentEnd exit code 2 → stdout is error reason, rc=1."""
        hook = _make_executable(tmp_path, "post.sh",
            'echo "tests failed, see report"; exit 2')
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        rc, _, _ = _run_in_dir(tmp_path, _run_agent([turn1], on_agent_end=hook))
        assert rc == 1

    def test_nonexistent_script_skipped(self, tmp_path):
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        rc, _, _ = _run_in_dir(tmp_path, _run_agent(
            [turn1], on_agent_end="./nonexistent.sh",
        ))
        assert rc == 0

    def test_stdout_emitted_without_crash(self, tmp_path):
        """onAgentEnd with non-empty stdout must not crash run_agent.

        Regression for BUG-1: ``hub.on_session_end()`` closed the log file
        before the hook ran, so emitting the hook's stdout afterwards raised
        ``ValueError: I/O operation on closed file``. This test sets up a real
        EventHub with a LogSubscriber (which opens and closes a file) to
        exercise that exact path. With the fix, the hook runs before
        on_session_end, so its output is emitted while the log is still open.
        """
        from letscode.events import EventHub, set_hub, get_hub, LogSubscriber
        hub = EventHub()
        log_sub = LogSubscriber(tmp_path / "lg")
        hub.subscribe(log_sub)
        set_hub(hub)
        try:
            hook = _make_executable(tmp_path, "post.sh",
                'echo "All tests passed."')
            turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
            rc, _, _ = _run_in_dir(tmp_path, _run_agent([turn1], on_agent_end=hook))
            # The crash (ValueError: I/O operation on closed file) would surface
            # as a non-zero rc / exception. rc==0 proves the ordering fix works.
            assert rc == 0
        finally:
            set_hub(None)  # clear the singleton so it doesn't leak across tests

    def test_config_path_threaded_to_hook_env(self, tmp_path):
        """config_path is exported as LETSCODE_CONFIG for hook scripts."""
        out = tmp_path / "env.txt"
        hook = _make_executable(tmp_path, "post.sh",
            f'printf "%s" "$LETSCODE_CONFIG" > {out}')
        turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)
        _run_in_dir(tmp_path, _run_agent(
            [turn1], on_agent_end=hook, config_path="/some/config.json",
        ))
        assert out.read_text() == "/some/config.json"

    def test_onagentend_runs_without_sandbox(self, tmp_path):
        """onAgentEnd hook runs with sandbox=False to avoid nested sandbox_apply.

        macOS Seatbelt does not allow nested sandbox_apply — build tools like
        swiftc internally call sandbox_apply, which fails inside an outer
        sandbox-exec. So onAgentEnd must run unsandboxed; sub-agents spawned by
        the hook apply their own card-level sandboxes.
        """
        from letscode.hooks import HookResult
        captured = {}

        def spy_run_hook(script, stdin_data, cwd, preset="default", sandbox=True, **kw):
            captured["sandbox"] = sandbox
            return HookResult(stdout="", returncode=0, skipped=False)

        async def _go():
            config = _make_config()
            config.preset = "safe"
            runner = _ScriptedRunner()
            msg_sub = MessageSubscriber()
            call_idx = [0]
            turn1 = StreamResult(text_content="done", tool_calls=[], usage=None)

            async def _mock_consume_stream(*args, **kwargs):
                idx = call_idx[0]
                call_idx[0] += 1
                if idx >= 1:
                    return StreamResult(text_content="(done)", tool_calls=[], usage=None)
                return turn1

            with patch("letscode.agent.consume_stream_async", side_effect=_mock_consume_stream):
                with patch("letscode.agent.apply_cache_markers", side_effect=lambda m, c: m):
                    with patch("letscode.hooks.run_hook", side_effect=spy_run_hook):
                        await run_agent(
                            prompt_blocks=[{"type": "text", "text": "t"}],
                            system_prompt="t", config=config, max_turns=10,
                            tool_runner=runner, msg_sub=msg_sub,
                            on_agent_end="./post.sh",
                        )

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            asyncio.run(_go())
        finally:
            os.chdir(old_cwd)
        assert captured.get("sandbox") is False, \
            "onAgentEnd must run without sandbox (nested sandbox_apply breaks build tools)"
