"""Tests for ``session/resume`` and ``session/close`` (unstable ACP methods).

``session/resume`` restores an agent's internal session state WITHOUT replaying
conversation history — for clients that already hold the history (e.g. chat
front ends). ``session/close`` terminates any running subprocess and drops all
per-session state. Both are routed by the SDK under ``unstable=True``, so
``use_unstable_protocol=True`` must be set on ``run_agent`` (verified separately).

These tests build a minimal ``LetscodeAgent`` (via ``__new__``, bypassing
``__init__``) and exercise the methods directly.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from acp.schema import CloseSessionResponse, ResumeSessionResponse

from letscode.acp.server import LetscodeAgent
from letscode.acp.session import Session, create_session, save_session


def _make_agent() -> LetscodeAgent:
    """Build a minimal LetscodeAgent without running the full __init__."""
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


def _write_log(cwd: str, session_id: str, events: list[dict]) -> str:
    """Write a JSONL session log under <cwd>/.letscode/sessions/ and return path."""
    log_dir = Path(cwd) / ".letscode" / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{session_id}.jsonl"
    with open(log_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return str(log_path)


def _result_event(prompt_tokens: int) -> dict:
    return {
        "type": "session/result",
        "data": {"usage": {"prompt_tokens": prompt_tokens}, "duration_ms": 1000},
    }


# ── resume_session ──

class TestResumeSession:
    def test_missing_session_returns_none(self, tmp_path):
        agent = _make_agent()
        resp = asyncio.run(agent.resume_session(
            cwd=str(tmp_path), session_id="nonexistent",
        ))
        assert resp is None

    def test_restores_session_into_self_sessions(self, tmp_path):
        agent = _make_agent()
        session = create_session(str(tmp_path))
        # Simulate a session that has been prompted (has a log).
        session.log_path = _write_log(str(tmp_path), session.session_id, [_result_event(500)])
        save_session(session)

        resp = asyncio.run(agent.resume_session(
            cwd=str(tmp_path), session_id=session.session_id,
        ))

        assert isinstance(resp, ResumeSessionResponse)
        # Session is registered for subsequent prompt() calls.
        assert session.session_id in agent.sessions
        assert agent.sessions[session.session_id].session_id == session.session_id

    def test_recovers_prompt_tokens_without_replay(self, tmp_path):
        """resume reads the log for the usage gauge but does NOT stream events."""
        agent = _make_agent()
        session = create_session(str(tmp_path))
        session.log_path = _write_log(str(tmp_path), session.session_id, [
            {"type": "agent_message_chunk", "data": {"text": "hello world"}},
            _result_event(42),
        ])
        save_session(session)

        asyncio.run(agent.resume_session(cwd=str(tmp_path), session_id=session.session_id))

        # The prompt_tokens were recovered (for the usage gauge).
        assert agent._session_prompt_tokens.get(session.session_id) == 42

    def test_does_not_replay_history(self, tmp_path):
        """The defining difference from load: no session_update calls for history.

        With ``_conn = None`` (our minimal agent), load_session's replay loop
        would be a no-op anyway, but we assert the stronger invariant: resume
        never even attempts ``session_update``. We wire a fake connection that
        records calls — resume must produce zero.
        """
        agent = _make_agent()
        session = create_session(str(tmp_path))
        session.log_path = _write_log(str(tmp_path), session.session_id, [
            {"type": "agent_message_chunk", "data": {"text": "should NOT be sent"}},
            _result_event(10),
        ])
        save_session(session)

        calls: list = []

        class _FakeConn:
            async def session_update(self, **kw):
                calls.append(kw)

        agent._conn = _FakeConn()
        asyncio.run(agent.resume_session(cwd=str(tmp_path), session_id=session.session_id))
        # No history replay — zero session_update calls (the deferred_commands
        # task fires after a 0.2s sleep; we don't await it here).
        assert calls == []

    def test_returns_config_options_and_modes(self, tmp_path):
        agent = _make_agent()
        session = create_session(str(tmp_path))
        save_session(session)

        resp = asyncio.run(agent.resume_session(
            cwd=str(tmp_path), session_id=session.session_id,
        ))
        assert resp is not None
        assert resp.config_options is not None
        assert any(o.id == "mode" for o in resp.config_options)
        assert resp.modes is not None
        # Round-trips through wire JSON a client SDK validates against.
        wire = resp.model_dump_json(by_alias=True)
        ResumeSessionResponse.model_validate_json(wire)

    def test_handles_never_prompted_session(self, tmp_path):
        """A session with no log_path (created but never prompted) resumes fine."""
        agent = _make_agent()
        session = create_session(str(tmp_path))
        save_session(session)  # no log_path set

        resp = asyncio.run(agent.resume_session(
            cwd=str(tmp_path), session_id=session.session_id,
        ))
        assert resp is not None
        assert session.session_id in agent.sessions
        # No prompt_tokens recovered (no log).
        assert session.session_id not in agent._session_prompt_tokens

    def test_accepts_additional_directories_and_mcp_servers(self, tmp_path):
        """Protocol sends these fields; resume must accept (and ignore) them."""
        agent = _make_agent()
        session = create_session(str(tmp_path))
        save_session(session)

        resp = asyncio.run(agent.resume_session(
            cwd=str(tmp_path),
            session_id=session.session_id,
            additional_directories=["/extra/dir"],
            mcp_servers=[],
        ))
        assert resp is not None


# ── close_session ──

class TestCloseSession:
    def test_removes_session_from_registry(self, tmp_path):
        agent = _make_agent()
        session = create_session(str(tmp_path))
        agent.sessions[session.session_id] = session

        asyncio.run(agent.close_session(session_id=session.session_id))

        assert session.session_id not in agent.sessions

    def test_clears_all_per_session_state(self, tmp_path):
        agent = _make_agent()
        sid = "s1"
        # Populate every per-session dict.
        agent.sessions[sid] = create_session(str(tmp_path))
        agent._session_commands[sid] = None
        agent._session_context_window[sid] = 200000
        agent._session_prompt_tokens[sid] = 500
        agent._session_big_turn[sid] = 3
        agent._session_title_task[sid] = None

        asyncio.run(agent.close_session(session_id=sid))

        assert sid not in agent.sessions
        assert sid not in agent._session_commands
        assert sid not in agent._session_context_window
        assert sid not in agent._session_prompt_tokens
        assert sid not in agent._session_big_turn
        assert sid not in agent._session_title_task

    def test_close_unknown_session_is_noop(self):
        agent = _make_agent()
        # Closing a non-existent id should not raise.
        result = asyncio.run(agent.close_session(session_id="ghost"))
        assert result == {}

    def test_returns_empty_dict(self, tmp_path):
        agent = _make_agent()
        session = create_session(str(tmp_path))
        agent.sessions[session.session_id] = session

        result = asyncio.run(agent.close_session(session_id=session.session_id))
        # close_session's route uses normalize_result; {} validates as
        # CloseSessionResponse (all-optional fields).
        assert result == {}
        CloseSessionResponse.model_validate(result)
