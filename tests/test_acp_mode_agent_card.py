"""Tests for exposing AgentCard selection via the ACP mode dropdown.

The plan (`docs/plan-agentcard-via-acp.md`) merges card selection into the
existing mode dropdown (3 sandbox presets + card stems) across both redundant
ACP surfaces: the top-level ``modes`` field (`_build_modes` +
`set_session_mode`) and the ``configOptions[mode]`` item
(`_build_config_options` + `set_config_option("mode")`).

The two paths share one dispatch helper (`_apply_mode_selection`) with
mutually-exclusive semantics: a preset id sets ``mode`` and clears
``agent_card``; a card stem sets ``agent_card`` and resets ``mode`` to
``default`` so the card's own preset takes effect.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from letscode.acp.server import LetscodeAgent
from letscode.acp.session import Session


def _make_agent() -> LetscodeAgent:
    """Build a minimal LetscodeAgent without running the full __init__.

    The builder methods only read ``self._models`` (falsy → model/effort
    branches skipped). The prompt()-based argv tests additionally need the
    per-session bookkeeping dicts that __init__ normally populates.
    """
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
    agent._cancelled = False
    agent._agent_proc = None
    agent._current_session_id = None
    agent._scan_dirs = []
    return agent


def _make_session(session_id: str = "s1", cwd: str = "/tmp") -> Session:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Session(session_id=session_id, cwd=cwd, created_at=now)


# A project agents/ dir with one card. The builtin cards (Explore/Plan/Review/
# SetupZed) are always discovered, so a project card "Foobar" is used to assert
# project-only presence without coupling to builtin content.
_PROJECT_CARD = """\
---
name: Foobar
description: A test card
tools: [Read]
---
You are Foobar.
"""


def _make_agent_with_card_cwd(tmp_path) -> LetscodeAgent:
    """Agent whose session cwd contains a project ``agents/Foobar.md`` card."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "Foobar.md").write_text(_PROJECT_CARD, encoding="utf-8")
    return _make_agent()


class TestBuildOptionsAndModes:
    def test_config_options_with_cards(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        mode_opt = next(
            o for o in agent._build_config_options(sess) if o.id == "mode"
        )
        values = {o.value for o in mode_opt.options}
        # 3 presets + the builtin cards + the project Foobar card.
        assert {"safe", "default", "risk"} <= values
        assert "foobar" in values
        # builtin cards are also surfaced
        assert "explore" in values
        # Card display names are prefixed with "Agent:" while the wire value
        # (stem) stays raw so dispatch still works.
        by_value = {o.value: o for o in mode_opt.options}
        assert by_value["foobar"].name == "Agent: Foobar"
        assert by_value["explore"].name == "Agent: Explore"
        # Presets are NOT prefixed.
        assert by_value["safe"].name == "Safe"

    def test_config_options_without_cards(self):
        # cwd with no agents/ dir: only the 3 presets (+ builtins, which ship
        # with the package). Project-namespace assertion: no "foobar".
        agent = _make_agent()
        sess = _make_session(cwd="/tmp")
        mode_opt = next(
            o for o in agent._build_config_options(sess) if o.id == "mode"
        )
        values = {o.value for o in mode_opt.options}
        assert {"safe", "default", "risk"} <= values
        assert "foobar" not in values

    def test_modes_with_cards(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        state = agent._build_modes(sess)
        ids = {m.id for m in state.available_modes}
        assert {"safe", "default", "risk"} <= ids
        assert "foobar" in ids
        # current_mode_id defaults to session.mode (no card selected)
        assert state.current_mode_id == "default"
        # Card entries show the Agent: prefix on their display name; id stays raw.
        by_id = {m.id: m for m in state.available_modes}
        assert by_id["foobar"].name == "Agent: Foobar"
        assert by_id["safe"].name == "Safe"

    def test_current_value_reflects_agent_card(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.agent_card = "foobar"
        mode_opt = next(
            o for o in agent._build_config_options(sess) if o.id == "mode"
        )
        assert mode_opt.current_value == "foobar"
        state = agent._build_modes(sess)
        assert state.current_mode_id == "foobar"


class TestSetSessionModeDispatch:
    def test_preset_sets_mode_clears_card(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.agent_card = "foobar"
        agent.sessions[sess.session_id] = sess

        asyncio.run(agent.set_session_mode("safe", sess.session_id))

        assert sess.mode == "safe"
        assert sess.agent_card is None

    def test_card_sets_agent_card_resets_mode(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.mode = "safe"
        agent.sessions[sess.session_id] = sess

        asyncio.run(agent.set_session_mode("foobar", sess.session_id))

        assert sess.agent_card == "foobar"
        assert sess.mode == "default"

    def test_builtin_card_stem_dispatches(self, tmp_path):
        # Builtin cards (Explore) are discovered regardless of cwd.
        agent = _make_agent()
        sess = _make_session(cwd=str(tmp_path))
        agent.sessions[sess.session_id] = sess

        asyncio.run(agent.set_session_mode("explore", sess.session_id))

        assert sess.agent_card == "explore"
        assert sess.mode == "default"

    def test_unknown_value_returns_none_and_session_unchanged(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.mode = "risk"
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(agent.set_session_mode("nonexistent", sess.session_id))

        assert resp is None
        assert sess.mode == "risk"
        assert sess.agent_card is None

    def test_missing_session_returns_none(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        resp = asyncio.run(agent.set_session_mode("safe", "nope"))
        assert resp is None

    def test_selecting_preset_after_card_is_mutually_exclusive(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        agent.sessions[sess.session_id] = sess

        asyncio.run(agent.set_session_mode("foobar", sess.session_id))
        assert sess.agent_card == "foobar"
        asyncio.run(agent.set_session_mode("risk", sess.session_id))
        assert sess.agent_card is None
        assert sess.mode == "risk"


class TestSetConfigOptionModeDispatch:
    """`set_config_option("mode", v)` must dispatch identically to
    `set_session_mode(v)` — the two APIs are redundant expressions of the
    same dropdown."""

    def test_preset_via_config_option(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.agent_card = "foobar"
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(agent.set_config_option("mode", sess.session_id, "safe"))

        assert resp is not None
        assert sess.mode == "safe"
        assert sess.agent_card is None

    def test_card_via_config_option(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(agent.set_config_option("mode", sess.session_id, "foobar"))

        assert resp is not None
        assert sess.agent_card == "foobar"
        assert sess.mode == "default"
        # echoed config_options reflect the card selection
        mode_opt = next(o for o in resp.config_options if o.id == "mode")
        assert mode_opt.current_value == "foobar"

    def test_unknown_value_via_config_option_returns_none(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        agent.sessions[sess.session_id] = sess

        resp = asyncio.run(
            agent.set_config_option("mode", sess.session_id, "nonexistent")
        )
        assert resp is None


class TestApplyModeSelection:
    def test_preset_path(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        assert agent._apply_mode_selection(sess, "safe") is True
        assert sess.mode == "safe" and sess.agent_card is None

    def test_card_path(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        assert agent._apply_mode_selection(sess, "foobar") is True
        assert sess.agent_card == "foobar" and sess.mode == "default"

    def test_miss_returns_false(self, tmp_path):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.mode = "risk"
        assert agent._apply_mode_selection(sess, "nope") is False
        # session unchanged
        assert sess.mode == "risk" and sess.agent_card is None


class TestPromptArgv:
    """Capture the letscode subprocess argv via monkeypatch and assert the
    --as / --preset forwarding rules."""

    @pytest.fixture
    def captured_cmd(self, monkeypatch):
        """Patch create_subprocess_exec to capture argv and yield a fake proc.

        Returns a list that will hold the cmd tuple after prompt() runs.
        """
        captured: list[list[str]] = []

        class _FakeStream:
            """Mimics asyncio.StreamReader: read(n) returns byte chunks."""
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
            def __init__(self):
                self.pid = 1234
                self.stdout = _FakeStream([])
                self.stderr = _FakeStream([])
                self.returncode = 0

            async def wait(self):
                return 0

            def kill(self):
                pass

        async def _fake_exec(*args, **kwargs):
            captured.append(list(args))
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return captured

    async def _run_prompt(self, agent, sess):
        agent.sessions[sess.session_id] = sess
        agent._current_session_id = None
        # Pre-set a title so prompt()'s first-prompt title-generation task
        # (which would call the LLM / load config) is not scheduled.
        sess.title = "test"
        # Prompt blocks: one trivial text block.
        await agent.prompt(prompt=[{"type": "text", "text": "hi"}],
                           session_id=sess.session_id)

    def test_card_selected_forwards_dash_as(self, tmp_path, captured_cmd):
        agent = _make_agent_with_card_cwd(tmp_path)
        # _models is {} (falsy) but _build_config_options calls
        # _model_effort_options which needs _default_model. Provide minimal.
        sess = _make_session(cwd=str(tmp_path))
        sess.agent_card = "foobar"
        asyncio.run(self._run_prompt(agent, sess))
        cmd = captured_cmd[0]
        assert "--as" in cmd
        assert cmd[cmd.index("--as") + 1] == "foobar"
        # card resets mode to default, so --preset is NOT forwarded
        assert "--preset" not in cmd

    def test_preset_selected_forwards_dash_preset(self, tmp_path, captured_cmd):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        sess.mode = "safe"
        asyncio.run(self._run_prompt(agent, sess))
        cmd = captured_cmd[0]
        assert "--preset" in cmd
        assert cmd[cmd.index("--preset") + 1] == "safe"
        assert "--as" not in cmd

    def test_default_no_card_no_flags(self, tmp_path, captured_cmd):
        agent = _make_agent_with_card_cwd(tmp_path)
        sess = _make_session(cwd=str(tmp_path))
        asyncio.run(self._run_prompt(agent, sess))
        cmd = captured_cmd[0]
        assert "--preset" not in cmd
        assert "--as" not in cmd
