"""Tests for the LLM stream consumer's display ordering.

The key invariant: reasoning (thinking) must display BEFORE the answer text,
matching the model's actual stream order. GLM streams reasoning_content (often
with no trailing newline) followed by content; without an explicit flush, the
reasoning sat in a line buffer until stream end and displayed AFTER the answer
— a timing bug that looked like the model thinking last.
"""

import asyncio
from unittest.mock import MagicMock

from letscode.stream import _consume_stream_once_async, _normalize_usage


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


class _Usage:
    """Stand-in for openai's ``CompletionUsage``. Carries arbitrary fields and
    exposes ``model_dump()`` so ``_normalize_usage`` treats it like the real SDK
    object (which preserves extra provider fields via ``extra="allow"``)."""

    def __init__(self, **fields):
        self._fields = fields

    def model_dump(self):
        return dict(self._fields)


class TestNormalizeUsage:
    """Cache-field normalization across the four provider shapes we verified
    in ``docs/cache-probe-2026-07-05.md``.

    The contract: regardless of which provider field name carries the cache
    info, ``_normalize_usage`` returns a flat dict with
    ``cache_read_tokens`` / ``cache_write_tokens`` keys (0 when absent).
    """

    def test_deepseek_native_fields(self):
        # DeepSeek-v4: prompt_cache_hit_tokens / prompt_cache_miss_tokens.
        # Probe call #2 showed hit=1664, miss=76.
        u = _normalize_usage(_Usage(
            prompt_tokens=1740, completion_tokens=65, total_tokens=1805,
            prompt_cache_hit_tokens=1664, prompt_cache_miss_tokens=76,
            prompt_tokens_details={"cached_tokens": 1664},
        ))
        assert u["cache_read_tokens"] == 1664
        assert u["prompt_tokens"] == 1740
        assert u["completion_tokens"] == 65

    def test_qwen_explicit_cache_marker(self):
        # Qwen with cache_control: cache_creation on call #1, cached on call #2.
        # Probe showed prompt_tokens_details.cached_tokens=1578 on hit.
        u = _normalize_usage(_Usage(
            prompt_tokens=1598, completion_tokens=40, total_tokens=1638,
            prompt_tokens_details={
                "cached_tokens": 1578,
                "cache_creation_input_tokens": 0,
            },
        ))
        assert u["cache_read_tokens"] == 1578
        assert u["cache_write_tokens"] == 0

    def test_qwen_cache_creation(self):
        # First call: cache write (creation) reported, no read yet.
        u = _normalize_usage(_Usage(
            prompt_tokens=1598, completion_tokens=40, total_tokens=1638,
            prompt_tokens_details={
                "cached_tokens": 0,
                "cache_creation_input_tokens": 1578,
            },
        ))
        assert u["cache_read_tokens"] == 0
        assert u["cache_write_tokens"] == 1578

    def test_anthropic_shape(self):
        # Anthropic-native: cache_read_input_tokens / cache_creation_input_tokens
        # at the top level of usage.
        u = _normalize_usage(_Usage(
            prompt_tokens=1000, completion_tokens=200, total_tokens=1200,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=100,
        ))
        assert u["cache_read_tokens"] == 900
        assert u["cache_write_tokens"] == 100

    def test_glm_old_model_no_cache(self):
        # glm-4.6v-flash: field present but always 0 (model not supported).
        u = _normalize_usage(_Usage(
            prompt_tokens=1624, completion_tokens=120, total_tokens=1744,
            prompt_tokens_details={"cached_tokens": 0},
        ))
        assert u["cache_read_tokens"] == 0
        assert u["cache_write_tokens"] == 0

    def test_no_cache_fields_returns_zeros(self):
        # A provider that never reports cache stats (e.g. plain OpenAI).
        u = _normalize_usage(_Usage(
            prompt_tokens=500, completion_tokens=50, total_tokens=550,
        ))
        assert u["cache_read_tokens"] == 0
        assert u["cache_write_tokens"] == 0
        assert u["prompt_tokens"] == 500

    def test_total_falls_back_to_prompt_plus_completion(self):
        # Some providers omit total_tokens; we compute it from the two parts.
        u = _normalize_usage(_Usage(
            prompt_tokens=300, completion_tokens=30,
        ))
        assert u["total_tokens"] == 330

    def test_none_usage_is_safe(self):
        u = _normalize_usage(None)
        assert u["prompt_tokens"] == 0
        assert u["cache_read_tokens"] == 0

    def test_reasoning_tokens_extracted(self):
        # DeepSeek/Qwen report reasoning_tokens inside completion_tokens_details.
        u = _normalize_usage(_Usage(
            prompt_tokens=100, completion_tokens=80, total_tokens=180,
            completion_tokens_details={"reasoning_tokens": 57},
        ))
        assert u["reasoning_tokens"] == 57

    def test_stream_result_carries_normalized_usage(self):
        # End-to-end through the stream consumer: the final StreamResult.usage
        # must carry cache_read_tokens, not just the three legacy fields.
        chunks = [
            _Chunk(_Delta(content="hi\n"), usage=_Usage(
                prompt_tokens=1740, completion_tokens=65, total_tokens=1805,
                prompt_cache_hit_tokens=1664,
                prompt_tokens_details={"cached_tokens": 1664},
            )),
        ]
        client = _client_yielding(chunks)

        async def run():
            return await _consume_stream_once_async(client, "m", [], 100)
        res = asyncio.run(run())

        assert res.usage["cache_read_tokens"] == 1664
        assert res.usage["prompt_tokens"] == 1740


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
