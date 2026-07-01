"""Tests for session title generation."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from letscode.acp.titles import generate_title
from letscode.stream import StreamResult


class TestGenerateTitle:
    """generate_title takes user prompts only (no agent replies)."""

    def test_generates_title_from_single_prompt(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="Fix login bug", tool_calls=[])
            )):
                return await generate_title(["login is broken"], "m")
        title = asyncio.run(run())
        assert title == "Fix login bug"

    def test_multiple_prompts_joined(self):
        captured = {}

        async def fake_call_llm(*args, **kwargs):
            captured["blocks"] = args[0]
            return StreamResult(text_content="Multi-turn task", tool_calls=[])

        async def run():
            with patch("letscode.acp.titles.call_llm", new=fake_call_llm):
                return await generate_title(["fix login", "now add tests"], "m")
        asyncio.run(run())
        # Multiple prompts are joined as bullet list in the text block.
        text = captured["blocks"][0]["text"]
        assert "fix login" in text and "now add tests" in text

    def test_strips_quotes_and_takes_first_line(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content='"Add tests"\n\nextra notes', tool_calls=[])
            )):
                return await generate_title(["write tests"], "m")
        title = asyncio.run(run())
        assert title == "Add tests"

    def test_caps_length(self):
        long = "x" * 200
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content=long, tool_calls=[])
            )):
                return await generate_title(["q"], "m")
        title = asyncio.run(run())
        assert len(title) <= 60

    def test_empty_prompts_returns_none(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock()):
                return await generate_title([], "m")
        assert asyncio.run(run()) is None

    def test_only_whitespace_prompts_returns_none(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock()):
                return await generate_title(["  "], "m")
        assert asyncio.run(run()) is None

    def test_llm_failure_returns_none(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock(
                side_effect=RuntimeError("rate limit")
            )):
                return await generate_title(["hi"], "m")
        assert asyncio.run(run()) is None

    def test_empty_result_returns_none(self):
        async def run():
            with patch("letscode.acp.titles.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="", tool_calls=[])
            )):
                return await generate_title(["hi"], "m")
        assert asyncio.run(run()) is None

    def test_disables_thinking(self):
        captured = {}

        async def fake_call_llm(*args, **kwargs):
            captured["extra_body"] = kwargs.get("extra_body")
            return StreamResult(text_content="T", tool_calls=[])

        async def run():
            with patch("letscode.acp.titles.call_llm", new=fake_call_llm):
                return await generate_title(["hi"], "m")
        asyncio.run(run())
        assert captured["extra_body"] == {"enable_thinking": False}


class TestGenerateAndSetTitle:
    """The server method persists the title AND pushes it to the client."""

    def _agent(self, tmp_path):
        from letscode.acp.server import LetscodeAgent
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key": "k",
                                   "models": [{"model": "m"}]}}}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        agent = LetscodeAgent(str(p))
        agent._conn = MagicMock()
        agent._conn.session_update = AsyncMock()
        return agent

    def _session(self, agent, sid="s1"):
        from letscode.acp.session import Session
        s = Session(session_id=sid, cwd="/tmp", created_at="2026-01-01T00:00:00Z",
                    model="m")
        agent.sessions[sid] = s
        return s

    def test_pushes_session_info_update_on_success(self, tmp_path):
        agent = self._agent(tmp_path)
        self._session(agent)
        with patch("letscode.acp.titles.call_llm", new=AsyncMock(
            return_value=StreamResult(text_content="My Title", tool_calls=[])
        )):
            asyncio.run(agent._generate_and_set_title("s1", "user question"))
        assert agent._conn.session_update.called
        update = agent._conn.session_update.call_args.kwargs["update"]
        assert update.session_update == "session_info_update"
        assert update.title == "My Title"

    def test_no_push_when_title_generation_fails(self, tmp_path):
        agent = self._agent(tmp_path)
        self._session(agent)
        with patch("letscode.acp.titles.call_llm", new=AsyncMock(
            side_effect=RuntimeError("down")
        )):
            asyncio.run(agent._generate_and_set_title("s1", "user question"))
        assert not agent._conn.session_update.called

    def test_persists_title_to_session(self, tmp_path):
        agent = self._agent(tmp_path)
        s = self._session(agent)
        with patch("letscode.acp.titles.call_llm", new=AsyncMock(
            return_value=StreamResult(text_content="Saved Title", tool_calls=[])
        )), patch("letscode.acp.server.save_session") as mock_save:
            asyncio.run(agent._generate_and_set_title("s1", "user question"))
        assert s.title == "Saved Title"
        assert mock_save.called

    def test_gathers_prior_prompts_from_log(self, tmp_path):
        agent = self._agent(tmp_path)
        s = self._session(agent)
        # Write a log with one prior user prompt.
        log = tmp_path / "session.jsonl"
        log.write_text(json.dumps({
            "type": "prompt", "data": [{"type": "text", "text": "prior question"}],
        }) + "\n")
        s.log_path = str(log)
        captured = {}

        async def fake_call_llm(*args, **kwargs):
            captured["blocks"] = args[0]
            return StreamResult(text_content="T", tool_calls=[])

        async def run():
            with patch("letscode.acp.titles.call_llm", new=fake_call_llm):
                await agent._generate_and_set_title("s1", "current question")
        asyncio.run(run())
        text = captured["blocks"][0]["text"]
        assert "prior question" in text and "current question" in text


class TestHandleRename:
    """/rename <title> sets directly; /rename with no arg generates via LLM."""

    def _agent(self, tmp_path):
        from letscode.acp.server import LetscodeAgent
        cfg = {"providers": {"p": {"base_url": "http://x", "api_key": "k",
                                   "models": [{"model": "m"}]}}}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        agent = LetscodeAgent(str(p))
        agent._conn = MagicMock()
        agent._conn.session_update = AsyncMock()
        return agent

    def _session(self, agent, sid="s1"):
        from letscode.acp.session import Session
        s = Session(session_id=sid, cwd="/tmp", created_at="2026-01-01T00:00:00Z",
                    model="m")
        agent.sessions[sid] = s
        return s

    def test_rename_with_arg_sets_title_directly(self, tmp_path):
        agent = self._agent(tmp_path)
        s = self._session(agent)
        with patch("letscode.acp.server.save_session"):
            asyncio.run(agent._handle_rename("s1", "My Custom Title"))
        assert s.title == "My Custom Title"

    def test_rename_no_arg_generates_title_from_user_prompts(self, tmp_path):
        agent = self._agent(tmp_path)
        s = self._session(agent)
        # Provide a log with user prompts.
        log = tmp_path / "session.jsonl"
        log.write_text(
            json.dumps({"type": "prompt", "data": [{"type": "text", "text": "fix the bug"}]}) + "\n"
            + json.dumps({"type": "prompt", "data": [{"type": "text", "text": "add tests too"}]}) + "\n"
        )
        s.log_path = str(log)
        with patch("letscode.acp.titles.call_llm", new=AsyncMock(
            return_value=StreamResult(text_content="Generated", tool_calls=[])
        )), patch("letscode.acp.server.save_session"):
            asyncio.run(agent._handle_rename("s1", None))
        assert s.title == "Generated"

    def test_rename_no_arg_failure_keeps_existing_title(self, tmp_path):
        agent = self._agent(tmp_path)
        s = self._session(agent)
        s.title = "Old Title"
        with patch("letscode.acp.titles.call_llm", new=AsyncMock(
            side_effect=RuntimeError("down")
        )):
            asyncio.run(agent._handle_rename("s1", None))
        assert s.title == "Old Title"
