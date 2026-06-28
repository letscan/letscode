"""Tests for call_llm — the single-shot LLM call building block."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from letscode.llm import call_llm
from letscode.stream import StreamResult, ToolCall


def _config_file(tmp_path, model="m1", base_url="http://x", api_key="k", **model_fields):
    import json as _json
    cfg = {
        "default_model": model,
        "providers": {
            "p": {
                "base_url": base_url, "api_key": api_key,
                "models": [{"model": model, **model_fields}],
            }
        },
    }
    p = tmp_path / "config.json"
    p.write_text(_json.dumps(cfg))
    return str(p)


class TestCallLlm:
    """call_llm resolves config, builds messages from prompt_blocks, returns text."""

    def test_text_prompt_returns_text_content(self, tmp_path):
        path = _config_file(tmp_path)
        result_holder = {}

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="hello back", tool_calls=[])
            )) as m:
                r = await call_llm(
                    [{"type": "text", "text": "hi"}],
                    config_path=path,
                )
                result_holder["r"] = r
                result_holder["args"] = m.call_args

        asyncio.run(run())
        assert result_holder["r"].text_content == "hello back"

    def test_system_prompt_prepended_to_messages(self, tmp_path):
        path = _config_file(tmp_path)
        captured = {}

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="ok", tool_calls=[])
            )) as m:
                await call_llm(
                    [{"type": "text", "text": "hi"}],
                    system_prompt="You are a summarizer.",
                    config_path=path,
                )
                # call_args: (client, model, messages, max_tokens, tools=...)
                captured["messages"] = m.call_args.args[2]

        asyncio.run(run())
        msgs = captured["messages"]
        assert msgs[0] == {"role": "system", "content": "You are a summarizer."}
        assert msgs[1]["role"] == "user"
        # prompt_blocks → user message content
        assert msgs[1]["content"] == "hi"

    def test_no_system_prompt_omits_system_message(self, tmp_path):
        path = _config_file(tmp_path)
        captured = {}

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="ok", tool_calls=[])
            )) as m:
                await call_llm([{"type": "text", "text": "hi"}], config_path=path)
                captured["messages"] = m.call_args.args[2]

        asyncio.run(run())
        msgs = captured["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_image_block_becomes_image_url_part(self, tmp_path):
        path = _config_file(tmp_path)
        captured = {}

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="a cat", tool_calls=[])
            )) as m:
                await call_llm(
                    [{"type": "image", "data": "AAA=", "mime_type": "image/png"},
                     {"type": "text", "text": "what is this?"}],
                    config_path=path,
                )
                captured["messages"] = m.call_args.args[2]

        asyncio.run(run())
        user_content = captured["messages"][-1]["content"]
        assert isinstance(user_content, list)
        kinds = [p["type"] for p in user_content]
        assert "image_url" in kinds

    def test_model_id_resolves_separate_provider(self, tmp_path):
        # Two providers; model_id picks the second one's config.
        import json as _json
        cfg = {
            "providers": {
                "p1": {"base_url": "http://a", "api_key": "k1",
                       "models": [{"model": "m1", "max_tokens": 1000}]},
                "p2": {"base_url": "http://b", "api_key": "k2",
                       "models": [{"model": "m2", "max_tokens": 2000}]},
            }
        }
        p = tmp_path / "config.json"
        p.write_text(_json.dumps(cfg))
        captured = {}

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="ok", tool_calls=[])
            )) as m:
                await call_llm([{"type": "text", "text": "x"}], model_id="m2", config_path=str(p))
                # args: (client, model, messages, max_tokens, ...)
                captured["model"] = m.call_args.args[1]
                captured["max_tokens"] = m.call_args.args[3]

        asyncio.run(run())
        assert captured["model"] == "m2"
        assert captured["max_tokens"] == 2000

    def test_no_tools_passed(self, tmp_path):
        path = _config_file(tmp_path)

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="ok", tool_calls=[])
            )) as m:
                await call_llm([{"type": "text", "text": "hi"}], config_path=path)
                # tools kwarg must be empty (single-shot, no tool exposure)
                assert m.call_args.kwargs.get("tools") == []

        asyncio.run(run())

    def test_surfaces_tool_calls_for_caller(self, tmp_path):
        # call_llm doesn't execute tools, but it must surface any tool_calls
        # the model returned, so a caller can loop on them.
        path = _config_file(tmp_path)
        tcs = [ToolCall(id="1", name="Bash", arguments="{}")]

        async def run():
            with patch("letscode.llm.consume_stream_async", new=AsyncMock(
                return_value=StreamResult(text_content="", tool_calls=tcs)
            )):
                r = await call_llm([{"type": "text", "text": "run ls"}], config_path=path)
                assert r.tool_calls == tcs

        asyncio.run(run())
