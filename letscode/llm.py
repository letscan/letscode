"""Single-shot LLM call — the shared building block for one-off model uses.

A thin, stateless wrapper around ``consume_stream_async`` (the only streaming
LLM call) plus config resolution. It does NOT execute tools or run an agent
loop — callers that need tool use (e.g. a future ``/compact`` with tools) wrap
a loop around the returned ``StreamResult.tool_calls`` themselves.

Typical uses: vision-image recognition, session title/summary generation,
topic-change detection, context compaction.

    result = await call_llm(
        [{"type": "text", "text": "summarize: ..."}],
        system_prompt="You are a summarizer.",
        model_id="glm-4.6v-flash",
    )
    text = result.text_content
"""

import logging
import time

from openai import AsyncOpenAI

from .config import load_config
from .stream import StreamResult, consume_stream_async
from .subscribers import _prompt_message

logger = logging.getLogger("letscode-acp")


async def call_llm(
    prompt_blocks: list[dict],
    *,
    system_prompt: str = "",
    model_id: str | None = None,
    config_path: str | None = None,
    max_tokens: int | None = None,
    purpose: str = "",
    extra_body: dict | None = None,
) -> StreamResult:
    """One LLM call. Returns the streamed result (text + any tool_calls).

    - ``prompt_blocks`` are converted to the OpenAI user message via
      :func:`_prompt_message` (so image/image_ref blocks become ``image_url``
      parts — works for vision models).
    - ``model_id`` resolves the model's own provider (api_key/base_url) via
      :func:`load_config`; ``None`` uses ``default_model``.
    - No tools are passed, so the model can't request tool calls in the normal
      flow — but ``StreamResult.tool_calls`` is still surfaced for callers that
      want to opt into a tool loop later.
    - ``purpose`` is a short label (e.g. "title", "vision", "summary") included
      in diagnostic logs to identify which call_llm invocation this was.
    """
    config, _ = load_config(config_path, model_id)
    client = AsyncOpenAI(
        api_key=config.api_key or "dummy",
        base_url=config.base_url,
    )

    user_msg = _prompt_message(prompt_blocks)
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append(user_msg)

    effective_max = max_tokens or config.max_tokens
    tag = f"[{purpose}]" if purpose else "[call_llm]"
    logger.info(
        "%s calling model=%s max_tokens=%d input_msgs=%d",
        tag, config.model, effective_max, len(messages),
    )

    t0 = time.monotonic()
    result = await consume_stream_async(
        client, config.model, messages, effective_max,
        tools=[],  # single-shot: no tools exposed
        max_retries=config.max_retries,
        extra_body=extra_body,
    )
    elapsed = time.monotonic() - t0

    # Diagnose "thinking too much": compare reasoning vs actual output.
    usage = result.usage or {}
    logger.info(
        "%s done in %.1fs — text=%d chars, reasoning=%d chars, "
        "tokens(in=%d/out=%d/total=%d)",
        tag, elapsed,
        len(result.text_content), len(result.thought_content),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
    )
    if result.thought_content and not result.text_content:
        logger.warning(
            "%s produced only reasoning (%d chars), no answer text — "
            "consider passing extra_body={'enable_thinking': False}",
            tag, len(result.thought_content),
        )

    return result
