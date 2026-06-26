"""Stream consumer — pure LLM stream parsing with zero side effects."""

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import (
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

    text_content = ""
    thought_content = ""
    tc_accum: dict[int, dict[str, str]] = {}
    line_buf = ""
    thought_buf = ""
    usage: dict | None = None

    for chunk in response:
        # The final chunk carries usage with an empty choices list — capture
        # it before the skip below discards the chunk.
        if chunk.usage is not None:
            usage = {
                "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
            }

        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta

        # Line-buffered text output
        if delta.content:
            text_content += delta.content
            line_buf += delta.content
            # Force flush if line_buf exceeds size limit (no newline in sight)
            if len(line_buf) > _MAX_LINE_BUF:
                if on_line:
                    on_line(line_buf)
                line_buf = ""
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                if on_line:
                    on_line(line)

        # Line-buffered reasoning/thinking output (GLM reasoning_content,
        # DeepSeek, etc.). The field is undeclared on the SDK's ChoiceDelta
        # but preserved at runtime via extra="allow".
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            thought_content += reasoning
            thought_buf += reasoning
            if len(thought_buf) > _MAX_LINE_BUF:
                if on_thought_line:
                    on_thought_line(thought_buf)
                thought_buf = ""
            while "\n" in thought_buf:
                line, thought_buf = thought_buf.split("\n", 1)
                if on_thought_line:
                    on_thought_line(line)

        # Accumulate tool call fragments
        if delta.tool_calls:
            for tc_delta in chunk.choices[0].delta.tool_calls:
                idx = tc_delta.index
                if idx not in tc_accum:
                    tc_accum[idx] = {"id": "", "name": "", "arguments": ""}
                if tc_delta.id:
                    tc_accum[idx]["id"] = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        tc_accum[idx]["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        tc_accum[idx]["arguments"] += tc_delta.function.arguments

    # Flush remaining buffered text
    if line_buf and on_line:
        on_line(line_buf)
    if thought_buf and on_thought_line:
        on_thought_line(thought_buf)

    tool_calls = [
        ToolCall(
            id=tc_accum[i]["id"],
            name=tc_accum[i]["name"],
            arguments=tc_accum[i]["arguments"],
        )
        for i in sorted(tc_accum.keys())
    ]

    return StreamResult(
        text_content=text_content,
        tool_calls=tool_calls,
        thought_content=thought_content,
        usage=usage,
    )
