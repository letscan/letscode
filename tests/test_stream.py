"""Tests for the LLM stream consumer's display ordering.

The key invariant: reasoning (thinking) must display BEFORE the answer text,
matching the model's actual stream order. GLM streams reasoning_content (often
with no trailing newline) followed by content; without an explicit flush, the
reasoning sat in a line buffer until stream end and displayed AFTER the answer
— a timing bug that looked like the model thinking last.
"""

import asyncio
from unittest.mock import MagicMock

from letscode.stream import _consume_stream_once_async


class _Delta:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = None


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta, usage=None):
        self.choices = [_Choice(delta)]
        self.usage = usage


def _client_yielding(chunks):
    """A fake AsyncOpenAI whose create() returns an async generator of chunks."""
    client = MagicMock()

    async def fake_create(**kw):
        async def gen():
            for c in chunks:
                yield c
        return gen()

    client.chat.completions.create = fake_create
    return client


class TestReasoningBeforeAnswer:
    """Reasoning must flush to the display before the answer text begins."""

    def test_reasoning_without_newline_flushes_before_content(self):
        # The exact bug: reasoning has no '\n', so it stayed buffered until the
        # stream ended — displaying AFTER content. It must flush when content
        # begins.
        chunks = [
            _Chunk(_Delta(reasoning_content="用户问3+5")),
            _Chunk(_Delta(reasoning_content="答案是8")),
            _Chunk(_Delta(content="\n")),
            _Chunk(_Delta(content="8\n")),
        ]
        client = _client_yielding(chunks)
        displayed = []

        async def run():
            await _consume_stream_once_async(
                client, "m", [], 100,
                on_line=lambda t: displayed.append(("TEXT", t)),
                on_thought_line=lambda t: displayed.append(("THINK", t)),
            )
        asyncio.run(run())

        kinds = [k for k, _ in displayed]
        assert "THINK" in kinds and "TEXT" in kinds, displayed
        assert kinds.index("THINK") < kinds.index("TEXT"), \
            f"reasoning displayed after text: {displayed}"

    def test_reasoning_with_newlines_still_works(self):
        # Normal case: reasoning has newlines, flushes line-by-line as before.
        chunks = [
            _Chunk(_Delta(reasoning_content="step 1\n")),
            _Chunk(_Delta(reasoning_content="step 2\n")),
            _Chunk(_Delta(content="done\n")),
        ]
        client = _client_yielding(chunks)
        displayed = []

        async def run():
            await _consume_stream_once_async(
                client, "m", [], 100,
                on_line=lambda t: displayed.append(("TEXT", t)),
                on_thought_line=lambda t: displayed.append(("THINK", t)),
            )
        asyncio.run(run())

        kinds = [k for k, _ in displayed]
        assert kinds == ["THINK", "THINK", "TEXT"], displayed

    def test_text_only_no_reasoning(self):
        # No reasoning → only text lines, no stray flush.
        chunks = [
            _Chunk(_Delta(content="hello\n")),
            _Chunk(_Delta(content="world\n")),
        ]
        client = _client_yielding(chunks)
        displayed = []

        async def run():
            await _consume_stream_once_async(
                client, "m", [], 100,
                on_line=lambda t: displayed.append(("TEXT", t)),
                on_thought_line=lambda t: displayed.append(("THINK", t)),
            )
        asyncio.run(run())

        assert displayed == [("TEXT", "hello"), ("TEXT", "world")], displayed

    def test_interleaved_reasoning_then_content(self):
        # If the model interleaves (reasoning, content, more reasoning), each
        # reasoning run flushes before the content that follows it.
        chunks = [
            _Chunk(_Delta(reasoning_content="think A")),
            _Chunk(_Delta(content="answer A\n")),
            _Chunk(_Delta(reasoning_content="think B")),
            _Chunk(_Delta(content="answer B\n")),
        ]
        client = _client_yielding(chunks)
        displayed = []

        async def run():
            await _consume_stream_once_async(
                client, "m", [], 100,
                on_line=lambda t: displayed.append(("TEXT", t)),
                on_thought_line=lambda t: displayed.append(("THINK", t)),
            )
        asyncio.run(run())

        kinds = [k for k, _ in displayed]
        # THINK, TEXT, THINK, TEXT — order preserved per run
        assert kinds == ["THINK", "TEXT", "THINK", "TEXT"], displayed

    def test_result_carries_both_contents(self):
        # StreamResult should still hold the full reasoning + text, regardless
        # of display flushing.
        chunks = [
            _Chunk(_Delta(reasoning_content="thinking here")),
            _Chunk(_Delta(content="answer")),
        ]
        client = _client_yielding(chunks)

        async def run():
            return await _consume_stream_once_async(client, "m", [], 100)
        res = asyncio.run(run())

        assert res.thought_content == "thinking here"
        assert res.text_content == "answer"
