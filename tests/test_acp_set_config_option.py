"""Regression test for `set_config_option` response shape.

The ACP schema marks `SetSessionConfigOptionResponse.configOptions` as
mandatory (no default, not Optional). letscode-acp previously returned a
bare `{}`, which failed client-side `ValidationError` (reported by LetsBot
on every session setup). The success path must return a properly populated
`SetSessionConfigOptionResponse`; the SDK contract also permits `None` for
the early-exit branches (missing session, unknown config id).
"""

import asyncio
from datetime import datetime, timezone

import pytest
from acp.schema import SetSessionConfigOptionResponse

from letscode.acp.server import LetscodeAgent
from letscode.acp.session import Session


def _make_agent() -> LetscodeAgent:
    """Build a minimal LetscodeAgent without running the full __init__.

    `_build_config_options` only reads `self._models` (empty → model branch
    skipped) and `self._model_effort_options` (returns None when
    `_default_model` is falsy), so these four attributes are sufficient.
    """
    agent = LetscodeAgent.__new__(LetscodeAgent)
    agent.sessions = {}
    agent._conn = None
    agent._models = {}
    agent._default_model = None
    agent._session_context_window = {}
    return agent


def _make_session(session_id: str = "s1") -> Session:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Session(session_id=session_id, cwd="/tmp", created_at=now)


class TestSetConfigOptionResponse:
    def test_success_returns_populated_response(self):
        agent = _make_agent()
        sess = _make_session()
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(agent.set_config_option("mode", sess.session_id, "safe"))

        assert isinstance(resp, SetSessionConfigOptionResponse)
        # mode option is always present
        assert any(o.id == "mode" for o in resp.config_options)
        # round-trips through the wire JSON a client SDK validates against
        wire = resp.model_dump_json(by_alias=True)
        assert "configOptions" in wire
        re_validated = SetSessionConfigOptionResponse.model_validate_json(wire)
        assert len(re_validated.config_options) == len(resp.config_options)
        # the echoed value reflects the change
        mode_opt = next(o for o in resp.config_options if o.id == "mode")
        assert mode_opt.current_value == "safe"

    def test_unknown_config_id_returns_none(self):
        agent = _make_agent()
        sess = _make_session()
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(agent.set_config_option("bogus", sess.session_id, "x"))

        assert resp is None

    def test_missing_session_returns_none(self):
        agent = _make_agent()

        resp = asyncio.run(agent.set_config_option("mode", "nonexistent", "safe"))

        assert resp is None
