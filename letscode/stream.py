"""Stream consumer — pure LLM stream parsing with zero side effects."""

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import (
    AsyncOpenAI,
    OpenAI,
    APIConnectionError,
    APIError,
    APITimeoutError,
    BadRequestError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class StreamResult:
    text_content: str
    tool_calls: list[ToolCall]
    thought_content: str = ""
    usage: dict | None = None


_MAX_LINE_BUF = 100_000

# Exceptions that are never worth retrying: client-side / auth / bad-request.
# Note: APIStatusError itself is NOT here — only its non-retryable subclasses.
_NON_RETRYABLE = (
    BadRequestError,
    AuthenticationError,
    PermissionDeniedError,
)

# Retryable exceptions: transient transport errors, rate limits, server faults.
_RETRYABLE = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 2s, 4s, 8s ..."""
    return 2.0 * (2 ** attempt)


def consume_stream(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_thought_line: Callable[[str], None] | None = None,
    max_retries: int = 3,
) -> StreamResult:
    """Consume a streaming LLM response with retry on transient failures.

    Calls on_line(text) for each complete line of text output.
    Calls on_thought_line(text) for each complete line of reasoning/thinking
    output (e.g. GLM's reasoning_content field).
    Returns StreamResult with full text content, thought content, accumulated
    tool calls, and token usage (when the server reports it).

    Retry policy: retries on rate-limit, timeout, connection, and 5xx errors
    with exponential backoff. Non-retryable errors (4xx except 429) propagate
    immediately. Retry is internal so a partially-streamed turn never leaks
    incomplete output to subscribers — accumulators reset on each attempt.
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _consume_stream_once(
                client, model, messages, max_tokens,
                tools=tools, on_line=on_line, on_thought_line=on_thought_line,
            )
        except _NON_RETRYABLE:
            # Client/ auth / bad-request errors: never retry.
            raise
        except _RETRYABLE as e:
            last_err = e
            if attempt >= max_retries:
                break
            wait = _backoff_seconds(attempt)
            label = type(e).__name__
            print(
                f"\n[retry {attempt + 1}/{max_retries}] {label}, "
                f"backing off {wait:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
        except APIError as e:
            # Other APIStatusError subclasses (4xx other than 429/400/401/403):
            # retry conservatively only if it looks like a server-side fault.
            code = getattr(e, "status_code", None) or 0
            if code >= 500 and attempt < max_retries:
                last_err = e
                wait = _backoff_seconds(attempt)
                print(
                    f"\n[retry {attempt + 1}/{max_retries}] HTTP {code}, "
                    f"backing off {wait:.0f}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise
    # Retries exhausted
    raise last_err  # type: ignore[misc]


async def consume_stream_async(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_thought_line: Callable[[str], None] | None = None,
    max_retries: int = 3,
    extra_body: dict | None = None,
) -> StreamResult:
    """Async variant of consume_stream. Awaits each chunk so the event loop
    stays responsive — Ctrl-C cancels the stream promptly instead of waiting
    for the whole LLM response to finish. Same retry policy as the sync version.

    ``extra_body`` is forwarded to the OpenAI SDK's ``create(extra_body=...)``,
    used for vendor extensions like ``{"enable_thinking": False}`` (Qwen3) to
    disable thinking mode for short-output calls (title/summary generation).
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _consume_stream_once_async(
                client, model, messages, max_tokens,
                tools=tools, on_line=on_line, on_thought_line=on_thought_line,
                extra_body=extra_body,
            )
        except _NON_RETRYABLE:
            raise
        except _RETRYABLE as e:
            last_err = e
            if attempt >= max_retries:
                break
            wait = _backoff_seconds(attempt)
            label = type(e).__name__
            print(
                f"\n[retry {attempt + 1}/{max_retries}] {label}, "
                f"backing off {wait:.0f}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(wait)
        except APIError as e:
            code = getattr(e, "status_code", None) or 0
            if code >= 500 and attempt < max_retries:
                last_err = e
                wait = _backoff_seconds(attempt)
                print(
                    f"\n[retry {attempt + 1}/{max_retries}] HTTP {code}, "
                    f"backing off {wait:.0f}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(wait)
            else:
                raise
    raise last_err  # type: ignore[misc]


def _normalize_usage(usage) -> dict:
    """Extract a stable usage dict from one provider's ``CompletionUsage``.

    Normalizes the prompt-cache fields across providers into a single pair:
    ``cache_read_tokens`` (cache HIT — tokens served from cache) and
    ``cache_write_tokens`` (cache CREATION — tokens written to the cache,
    often billed at a premium). Both default to 0 when the provider doesn't
    report cache stats.

    Provider field-name variants handled here:
      - OpenAI / Qwen / GLM: ``prompt_tokens_details.cached_tokens`` (read)
      - DeepSeek-native: ``prompt_cache_hit_tokens`` (read) /
        ``prompt_cache_miss_tokens`` (≈ write, the non-cached prefix)
      - Anthropic: ``cache_read_input_tokens`` (read) /
        ``cache_creation_input_tokens`` (write)

    The SDK preserves extra provider fields at runtime via ``extra="allow"``,
    so the non-OpenAI fields are reachable through ``model_dump()``.
    """
    raw = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage or {})

    prompt = raw.get("prompt_tokens", 0) or 0
    completion = raw.get("completion_tokens", 0) or 0
    total = raw.get("total_tokens", 0) or (prompt + completion)

    ptd = raw.get("prompt_tokens_details") or {}
    ctd = raw.get("completion_tokens_details") or {}

    # Cache reads (hits). Try every known field name; first non-zero wins.
    cache_read = (
        ptd.get("cached_tokens")
        or raw.get("prompt_cache_hit_tokens")
        or raw.get("cache_read_input_tokens")
        or 0
    ) or 0

    # Cache writes (creation). DeepSeek has no explicit write field, so we use
    # prompt_cache_miss_tokens (the prefix that wasn't cached this turn) as an
    # upper-bound proxy only when there was no hit — this avoids double-counting
    # on the common steady-state path where most of the prefix is cached.
    cache_write = (
        ptd.get("cache_creation_input_tokens")
        or raw.get("cache_creation_input_tokens")
        or 0
    ) or 0

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": getattr(ctd, "reasoning_tokens", None) or ctd.get("reasoning_tokens") or 0,
    }


def _process_chunk(chunk, state: dict,
                   on_line: Callable[[str], None] | None,
                   on_thought_line: Callable[[str], None] | None) -> None:
    """Fold one stream chunk into the accumulator state (shared by sync/async).

    Mutates ``state`` in place: text_content, thought_content, usage, line_buf,
    thought_buf, tc_accum. Side effects: calls on_line / on_thought_line.
    """
    if chunk.usage is not None:
        state["usage"] = _normalize_usage(chunk.usage)

    if not chunk.choices:
        return

    delta = chunk.choices[0].delta

    # Line-buffered text output.
    # Models stream reasoning first, then the answer (GLM: reasoning_content
    # then content). Reasoning often has no trailing newline, so it would sit
    # in thought_buf until stream end and display AFTER the answer. Flush any
    # pending reasoning the moment answer text begins, preserving real order.
    if delta.content:
        if state["thought_buf"] and on_thought_line:
            on_thought_line(state["thought_buf"])
            state["thought_buf"] = ""

        state["text_content"] += delta.content
        state["line_buf"] += delta.content
        if len(state["line_buf"]) > _MAX_LINE_BUF:
            if on_line:
                on_line(state["line_buf"])
            state["line_buf"] = ""
        while "\n" in state["line_buf"]:
            line, state["line_buf"] = state["line_buf"].split("\n", 1)
            if on_line:
                on_line(line)

    # Line-buffered reasoning/thinking output (GLM reasoning_content, DeepSeek,
    # etc.). The field is undeclared on the SDK's ChoiceDelta but preserved at
    # runtime via extra="allow".
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        state["thought_content"] += reasoning
        state["thought_buf"] += reasoning
        if len(state["thought_buf"]) > _MAX_LINE_BUF:
            if on_thought_line:
                on_thought_line(state["thought_buf"])
            state["thought_buf"] = ""
        while "\n" in state["thought_buf"]:
            line, state["thought_buf"] = state["thought_buf"].split("\n", 1)
            if on_thought_line:
                on_thought_line(line)

    # Accumulate tool call fragments
    if delta.tool_calls:
        for tc_delta in chunk.choices[0].delta.tool_calls:
            idx = tc_delta.index
            if idx not in state["tc_accum"]:
                state["tc_accum"][idx] = {"id": "", "name": "", "arguments": ""}
            if tc_delta.id:
                state["tc_accum"][idx]["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    state["tc_accum"][idx]["name"] = tc_delta.function.name
                if tc_delta.function.arguments:
                    state["tc_accum"][idx]["arguments"] += tc_delta.function.arguments


def _new_state() -> dict:
    return {
        "text_content": "",
        "thought_content": "",
        "usage": None,
        "line_buf": "",
        "thought_buf": "",
        "tc_accum": {},
    }


def _state_to_result(state: dict) -> StreamResult:
    tool_calls = [
        ToolCall(
            id=state["tc_accum"][i]["id"],
            name=state["tc_accum"][i]["name"],
            arguments=state["tc_accum"][i]["arguments"],
        )
        for i in sorted(state["tc_accum"].keys())
    ]
    return StreamResult(
        text_content=state["text_content"],
        tool_calls=tool_calls,
        thought_content=state["thought_content"],
        usage=state["usage"],
    )


def _flush_buffers(state: dict,
                   on_line: Callable[[str], None] | None,
                   on_thought_line: Callable[[str], None] | None) -> None:
    if state["line_buf"] and on_line:
        on_line(state["line_buf"])
    if state["thought_buf"] and on_thought_line:
        on_thought_line(state["thought_buf"])


def _consume_stream_once(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_thought_line: Callable[[str], None] | None = None,
) -> StreamResult:
    """Single streaming attempt. Raises on any error; no retry here."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or [],
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )

    state = _new_state()
    for chunk in response:
        _process_chunk(chunk, state, on_line, on_thought_line)
    _flush_buffers(state, on_line, on_thought_line)
    return _state_to_result(state)


async def _consume_stream_once_async(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_thought_line: Callable[[str], None] | None = None,
    extra_body: dict | None = None,
) -> StreamResult:
    """Async single streaming attempt. Awaits each chunk so the event loop
    stays responsive to cancellation (Ctrl-C) mid-stream."""
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or [],
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
        **({"extra_body": extra_body} if extra_body else {}),
    )

    state = _new_state()
    async for chunk in response:
        _process_chunk(chunk, state, on_line, on_thought_line)
    _flush_buffers(state, on_line, on_thought_line)
    return _state_to_result(state)
