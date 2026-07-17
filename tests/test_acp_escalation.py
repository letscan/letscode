"""Tests for the ACP permission-escalation flow in server.py.

Covers the four-step flow described in docs/plan-permission-escalation.md:
1. Server intercepts ``permission_denied`` error code (does NOT raise).
2. Probe extracts the precise permission request (mocked).
3. ``request_permission`` popup returns approve / approve_for_session / reject.
4. Respawn argv includes ``--allow`` (allow-once) OR a session config file is
   written (allow-always) OR nothing happens (reject).
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from letscode.acp.server import LetscodeAgent, _SubprocessResult
from letscode.acp.session import Session, create_session


def _make_agent(tmp_path) -> LetscodeAgent:
    """Build a minimal LetscodeAgent without running __init__."""
    agent = LetscodeAgent.__new__(LetscodeAgent)
    agent.sessions = {}
    agent._conn = None
    agent.config_path = None
    agent.show_stat = False
    agent._models = {}
    agent._default_model = None
    agent._session_context_window = {}
    agent._session_commands = {}
    agent._session_title_task = {}
    agent._session_prompt_tokens = {}
    agent._session_big_turn = {}
    agent._scan_dirs = []
    agent._cancelled = False
    agent._agent_proc = None
    agent._current_session_id = None
    return agent


def _make_session(cwd: str, session_id: str = "s1") -> Session:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Session(session_id=session_id, cwd=cwd, created_at=now)


# ---------------------------------------------------------------------------
# Error-code interception: permission_denied is captured, not raised
# ---------------------------------------------------------------------------

class _AsyncLineIter:
    """Async iterator over a list of line strings (mimics subprocess stdout)."""
    def __init__(self, lines):
        self._lines = list(lines)
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0).encode()
        raise StopAsyncIteration


class _FakeProc:
    """Minimal asyncio subprocess stand-in for _run_agent_subprocess tests."""
    def __init__(self, lines):
        self.stdout = _AsyncLineIter(lines)
        self.returncode = 0
        self.pid = 12345
    async def wait(self):
        return self.returncode
    def kill(self):
        pass


class TestInterceptPermissionDenied:
    """_run_agent_subprocess reads the error event's code. permission_denied
    populates pending_denials and does NOT set error_msg (which would raise)."""

    def test_permission_denied_captures_denials(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        error_event_line = json.dumps({
            "type": "error",
            "data": {
                "message": "Permission escalation available: 1 tool call(s) denied",
                "code": "permission_denied",
                "recoverable": True,
                "denials": [{"type": "write", "target": "/etc/hosts"}],
            },
        }) + "\n"

        async def fake_spawn(*a, **kw):
            return _FakeProc([error_event_line])

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_spawn)):
            run = asyncio.run(agent._run_agent_subprocess(
                ["letscode"], session, session.session_id,
            ))

        assert run.pending_denials == [{"type": "write", "target": "/etc/hosts"}]
        # Crucially, error_msg is NOT set → no RequestError raised downstream.
        assert run.error_msg is None
        assert run.exit_code == 0

    def test_other_error_code_sets_error_msg(self, tmp_path):
        """Non-permission errors stay fatal — error_msg is set so prompt()
        raises RequestError."""
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        error_event_line = json.dumps({
            "type": "error",
            "data": {"message": "API key invalid", "code": "api_error"},
        }) + "\n"

        async def fake_spawn(*a, **kw):
            return _FakeProc([error_event_line])

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_spawn)):
            run = asyncio.run(agent._run_agent_subprocess(
                ["letscode"], session, session.session_id,
            ))
        assert run.error_msg == "API key invalid"
        assert run.pending_denials == []


# ---------------------------------------------------------------------------
# Escalation loop: approve → respawn with --allow
# ---------------------------------------------------------------------------

class TestEscalationApproveOnce:
    """User picks 'approve' (allow-once): respawn argv includes the precise
    --allow <type>:<target>, no session config written."""

    def test_approve_adds_allow_flag_to_respawn(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")  # empty feed is fine for probe mocking

        initial = _SubprocessResult(
            pending_denials=[{"type": "write", "target": "/etc/hosts"}],
        )

        # Probe returns a request; popup returns approve; respawn is a no-op
        # run (no further denials → loop ends).
        respawned = _SubprocessResult(pending_denials=[])

        spawn_cmds: list[list[str]] = []

        async def fake_run(cmd, sess, sid):
            spawn_cmds.append(list(cmd))
            return respawned

        async def fake_probe(*a, **kw):
            return {"type": "write", "target": "/etc/hosts", "reason": "r"}

        class _FakeOutcome:
            outcome = "selected"
            option_id = "approve"

        class _FakeResp:
            outcome = _FakeOutcome()

        class _FakeConn:
            async def request_permission(self, **kw):
                return _FakeResp()

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            result = asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        # Exactly one respawn happened.
        assert len(spawn_cmds) == 1
        respawn_argv = spawn_cmds[0]
        # The respawn includes --allow write:/etc/hosts
        allow_idx = respawn_argv.index("--allow")
        assert respawn_argv[allow_idx + 1] == "write:/etc/hosts"
        # And the fixed continuation prompt.
        text_idx = respawn_argv.index("--text")
        assert respawn_argv[text_idx + 1] == "权限已更新,请继续"
        # No session-level config file written for allow-once.
        assert not (tmp_path / ".letscode" / "config.s1.json").exists()


# ---------------------------------------------------------------------------
# Escalation loop: approve_for_session → generalized pattern persisted
# ---------------------------------------------------------------------------

class TestEscalationApproveForSession:
    def test_approve_for_session_writes_generalized_pattern(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(
            pending_denials=[{"type": "cmd", "target": "npm run dev"}],
        )
        respawned = _SubprocessResult(pending_denials=[])

        spawn_cmds: list[list[str]] = []

        async def fake_run(cmd, sess, sid):
            spawn_cmds.append(list(cmd))
            return respawned

        async def fake_probe(*a, **kw):
            return {"type": "cmd", "target": "npm run dev", "reason": "r"}

        class _FakeOutcome:
            outcome = "selected"
            option_id = "approve_for_session"

        class _FakeResp:
            outcome = _FakeOutcome()

        class _FakeConn:
            async def request_permission(self, **kw):
                return _FakeResp()

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        # Session config now has the generalized pattern: "npm run *" (cmd
        # with known subcommand generalizes to <cmd> <sub> *).
        cfg_path = tmp_path / ".letscode" / "config.s1.json"
        assert cfg_path.exists()
        data = json.loads(cfg_path.read_text())
        assert data == {"allowCmd": ["npm run *"]}

        # The respawn argv must NOT carry --allow (the allow-always is picked
        # up from the config file via the --feed-derived session id).
        respawn_argv = spawn_cmds[0]
        assert "--allow" not in respawn_argv


# ---------------------------------------------------------------------------
# Escalation loop: reject → no respawn, no config
# ---------------------------------------------------------------------------

class TestEscalationReject:
    def test_reject_no_respawn_no_config(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(
            pending_denials=[{"type": "write", "target": "/etc/hosts"}],
        )

        spawn_calls = 0

        async def fake_run(cmd, sess, sid):
            nonlocal spawn_calls
            spawn_calls += 1
            return _SubprocessResult()

        async def fake_probe(*a, **kw):
            return {"type": "write", "target": "/etc/hosts", "reason": "r"}

        # DeniedOutcome: outcome="cancelled" (no option_id)
        class _FakeOutcome:
            outcome = "cancelled"

        class _FakeResp:
            outcome = _FakeOutcome()

        class _FakeConn:
            async def request_permission(self, **kw):
                return _FakeResp()

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            result = asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        assert spawn_calls == 0
        assert not (tmp_path / ".letscode" / "config.s1.json").exists()
        # The initial run stands (returned).
        assert result is initial


# ---------------------------------------------------------------------------
# Probe says "not a permission block" → abort escalation
# ---------------------------------------------------------------------------

class TestProbeNegative:
    def test_probe_none_aborts_loop(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(
            pending_denials=[{"type": "write", "target": "/etc/hosts"}],
        )

        spawn_calls = 0

        async def fake_run(cmd, sess, sid):
            nonlocal spawn_calls
            spawn_calls += 1
            return _SubprocessResult()

        async def fake_probe(*a, **kw):
            return None  # probe: not actually blocked

        agent._conn = type("C", (), {"async_request_permission": None})()  # never called

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            result = asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        assert spawn_calls == 0
        # pending_denials cleared (so caller treats run as final).
        assert result.pending_denials == []


# ---------------------------------------------------------------------------
# Full prompt() integration: permission_denied → escalation → end
# ---------------------------------------------------------------------------

class TestPromptEscalationIntegration:
    """End-to-end prompt() with mocked subprocess + probe + popup. Verifies
    prompt() returns a PromptResponse without raising on permission_denied."""

    def test_prompt_returns_after_approval(self, tmp_path):
        agent = _make_agent(tmp_path)
        # Wire minimal session state.
        session = _make_session(str(tmp_path))
        session.model = None  # no model → no --model flag
        session.title = "already set"  # prevent title-gen task from firing
        agent.sessions[session.session_id] = session
        agent._session_commands[session.session_id] = None

        # Two subprocess runs: first emits permission_denied, second is clean.
        run1 = _SubprocessResult(
            pending_denials=[{"type": "write", "target": "/etc/hosts"}],
            stop_reason="end_turn",
        )
        run2 = _SubprocessResult(pending_denials=[], stop_reason="end_turn")

        async def fake_probe(*a, **kw):
            return {"type": "write", "target": "/etc/hosts", "reason": "r"}

        class _FakeOutcome:
            outcome = "selected"
            option_id = "approve"

        class _FakeResp:
            outcome = _FakeOutcome()

        class _FakeConn:
            async def session_update(self, **kw):
                pass
            async def request_permission(self, **kw):
                return _FakeResp()

        agent._conn = _FakeConn()

        # Stub _run_agent_subprocess to return our canned results in order.
        runs = iter([run1, run2])

        async def fake_run_seq(cmd, sess, sid):
            return next(runs)

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run_seq)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            resp = asyncio.run(agent.prompt(
                prompt=[{"type": "text", "text": "edit hosts"}],
                session_id=session.session_id,
            ))

        # No raise, returns a PromptResponse with the final run's stop_reason.
        assert resp.stop_reason == "end_turn"
