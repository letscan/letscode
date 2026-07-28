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
    """Async stream stand-in mimicking asyncio.StreamReader.

    Supports both ``read(n)`` (used by _read_jsonl_lines) and async iteration
    (legacy). Stores complete lines and feeds them as byte chunks.
    """
    def __init__(self, lines):
        self._buf = b"".join(
            l if isinstance(l, bytes) else l.encode() for l in lines
        )
    async def read(self, n: int = -1) -> bytes:
        if n < 0 or n >= len(self._buf):
            chunk, self._buf = self._buf, b""
            return chunk
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


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
            return {"permissions": [{"type": "write", "target": "/etc/hosts"}], "reason": "r"}

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
            return {"permissions": [{"type": "cmd", "target": "npm run dev"}], "reason": "r"}

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
# Escalation loop: multiple permissions → popup each, mixed approvals
# ---------------------------------------------------------------------------

class TestEscalationMultiplePermissions:
    """The probe returns a minimal set of distinct permissions; the server
    pops up each one and applies the user's per-permission decision. This
    mirrors the LetsBot delete scenario where the agent tried rm/mv/chmod/
    python/perl/sudo against the same target — probe collapses to one, but
    distinct targets each get their own popup."""

    def test_two_permissions_each_popped_individually(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(pending_denials=[
            {"type": "cmd", "target": "rm /a"},
            {"type": "cmd", "target": "rm /b"},
        ])
        respawned = _SubprocessResult(pending_denials=[])

        spawn_cmds: list[list[str]] = []
        popup_calls: list[dict] = []

        async def fake_run(cmd, sess, sid):
            spawn_cmds.append(list(cmd))
            return respawned

        async def fake_probe(*a, **kw):
            return {"permissions": [
                {"type": "cmd", "target": "rm /a"},
                {"type": "cmd", "target": "rm /b"},
            ], "reason": "delete two files"}

        class _FakeConn:
            async def request_permission(self, **kw):
                popup_calls.append(kw)
                # Approve both.
                class _O:
                    outcome = "selected"
                    option_id = "approve"
                class _R:
                    outcome = _O()
                return _R()

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        # Two popups fired (one per permission).
        assert len(popup_calls) == 2
        # One respawn, carrying both --allow flags.
        assert len(spawn_cmds) == 1
        argv = spawn_cmds[0]
        allow_flags = [argv[i + 1] for i, t in enumerate(argv) if t == "--allow"]
        assert "cmd:rm /a" in allow_flags
        assert "cmd:rm /b" in allow_flags
        assert len(allow_flags) == 2

    def test_mixed_decisions_approved_and_rejected(self, tmp_path):
        """User approves one permission, rejects the other: only the approved
        one lands in the respawn --allow flags."""
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(pending_denials=[
            {"type": "cmd", "target": "rm /a"},
            {"type": "cmd", "target": "sudo rm /b"},
        ])
        respawned = _SubprocessResult(pending_denials=[])

        spawn_cmds: list[list[str]] = []

        async def fake_run(cmd, sess, sid):
            spawn_cmds.append(list(cmd))
            return respawned

        async def fake_probe(*a, **kw):
            return {"permissions": [
                {"type": "cmd", "target": "rm /a"},
                {"type": "cmd", "target": "sudo rm /b"},
            ], "reason": "r"}

        class _FakeConn:
            def __init__(self):
                self._n = 0
            async def request_permission(self, **kw):
                self._n += 1
                class _O:
                    pass
                class _R:
                    pass
                r = _R()
                if self._n == 1:
                    # First popup (rm /a): approve.
                    o = _O(); o.outcome = "selected"; o.option_id = "approve"
                    r.outcome = o
                else:
                    # Second popup (sudo rm /b): reject.
                    o = _O(); o.outcome = "cancelled"
                    r.outcome = o
                return r

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        # Respawn carries only the approved --allow.
        assert len(spawn_cmds) == 1
        argv = spawn_cmds[0]
        allow_flags = [argv[i + 1] for i, t in enumerate(argv) if t == "--allow"]
        assert allow_flags == ["cmd:rm /a"]

    def test_all_rejected_no_respawn(self, tmp_path):
        """User rejects every permission: no respawn fires (the best-effort
        agent's 'I can't' text was already streamed)."""
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(pending_denials=[
            {"type": "cmd", "target": "rm /a"},
            {"type": "cmd", "target": "rm /b"},
        ])

        spawn_calls = 0

        async def fake_run(cmd, sess, sid):
            nonlocal spawn_calls
            spawn_calls += 1
            return _SubprocessResult()

        async def fake_probe(*a, **kw):
            return {"permissions": [
                {"type": "cmd", "target": "rm /a"},
                {"type": "cmd", "target": "rm /b"},
            ], "reason": "r"}

        class _FakeConn:
            async def request_permission(self, **kw):
                class _O:
                    outcome = "cancelled"
                class _R:
                    outcome = _O()
                return _R()

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        assert spawn_calls == 0

    def test_mixed_allow_once_and_allow_always(self, tmp_path):
        """One permission approved-for-session (persisted), another approved-once
        (--allow flag): both mechanisms applied in the same respawn."""
        agent = _make_agent(tmp_path)
        session = _make_session(str(tmp_path))
        log_path = tmp_path / ".letscode" / "sessions" / "s1.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")

        initial = _SubprocessResult(pending_denials=[
            {"type": "write", "target": "/a/b/c.txt"},
            {"type": "cmd", "target": "npm run dev"},
        ])
        respawned = _SubprocessResult(pending_denials=[])

        spawn_cmds: list[list[str]] = []

        async def fake_run(cmd, sess, sid):
            spawn_cmds.append(list(cmd))
            return respawned

        async def fake_probe(*a, **kw):
            return {"permissions": [
                {"type": "write", "target": "/a/b/c.txt"},
                {"type": "cmd", "target": "npm run dev"},
            ], "reason": "r"}

        class _FakeConn:
            def __init__(self):
                self._n = 0
            async def request_permission(self, **kw):
                self._n += 1
                class _O:
                    pass
                class _R:
                    pass
                r = _R()
                if self._n == 1:
                    # write /a/b/c.txt → approve_for_session (persisted).
                    o = _O(); o.outcome = "selected"; o.option_id = "approve_for_session"
                    r.outcome = o
                else:
                    # npm run dev → approve (allow-once flag).
                    o = _O(); o.outcome = "selected"; o.option_id = "approve"
                    r.outcome = o
                return r

        agent._conn = _FakeConn()

        with patch.object(agent, "_run_agent_subprocess", new=AsyncMock(side_effect=fake_run)), \
             patch("letscode.acp.permission.probe_permission_request", new=AsyncMock(side_effect=fake_probe)):
            asyncio.run(agent._escalation_loop(
                initial, session, session.session_id, log_path, None,
            ))

        # Respawn has the allow-once flag for npm, NOT for the write (that one
        # is picked up from config.<sid>.json).
        argv = spawn_cmds[0]
        allow_flags = [argv[i + 1] for i, t in enumerate(argv) if t == "--allow"]
        assert allow_flags == ["cmd:npm run dev"]
        # Session config has the generalized write pattern (/a/b/*).
        cfg = json.loads((tmp_path / ".letscode" / "config.s1.json").read_text())
        assert cfg == {"allowWrite": ["/a/b/*"]}

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
            return {"permissions": [{"type": "write", "target": "/etc/hosts"}], "reason": "r"}

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
            return {"permissions": [{"type": "write", "target": "/etc/hosts"}], "reason": "r"}

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
