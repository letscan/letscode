"""Probe GLM model cache support: glm-4.6v vs glm-4.7 vs glm-5.2.

Same long stable prefix, two back-to-back calls per model. The second call
should hit cache if the model supports it. Dumps the RAW provider usage dict
(no normalization) so we see exactly what each model returns.

Zhipu's official doc (https://docs.bigmodel.cn/cn/guide/capabilities/cache)
claims cache support for "GLM-5.2, GLM-5.1, GLM-5 series" and uses content
similarity matching. This probe verifies which GLM-4.x variants actually
populate cached_tokens with a meaningful value vs. a noise constant.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from letscode.config import load_config  # noqa: E402

# ~10 KB stable prefix, well above any cache threshold.
PREFIX = (
    "You are a meticulous senior software engineer working on the letscode "
    "project. Memorize the architecture guide below; later turns reference it.\n\n"
    "## Architecture\n"
) + (
    "The streaming core parses SSE chunks and folds them into accumulator state. "
    "The tool dispatcher maps schema names to executor functions via a flat dict. "
    "The event emitter writes JSONL records with a stable schema. "
    "The configuration layer flattens providers and models into per-model dicts. "
) * 30


async def probe(client, model: str) -> None:
    print(f"\n{'=' * 70}\n{model}\n{'=' * 70}")
    # Two identical calls back-to-back. We do NOT add cache_control markers —
    # Zhipu's doc says caching is automatic/implicit, so we test that path.
    msgs = [{"role": "system", "content": PREFIX},
            {"role": "user", "content": "In one short sentence: what does the streaming core do?"}]
    prev_cached = None
    for i in range(2):
        t0 = time.monotonic()
        try:
            r = await client.chat.completions.create(
                model=model, messages=msgs, max_tokens=40,
                stream=True, stream_options={"include_usage": True},
            )
        except Exception as e:
            print(f"  call #{i+1}: ERROR {type(e).__name__}: {str(e)[:120]}")
            return
        u = None
        async for ch in r:
            if ch.usage is not None:
                u = ch.usage.model_dump()
        dt = time.monotonic() - t0
        ptd = (u or {}).get("prompt_tokens_details") or {}
        ct = ptd.get("cached_tokens")
        print(f"  call #{i+1} ({dt:.2f}s): prompt={u.get('prompt_tokens')} "
              f"cached_tokens={ct!r}  full_ptd={json.dumps(ptd, ensure_ascii=False)}")
        # Quick verdict on call #2.
        if i == 1:
            if ct and ct > 100:
                print(f"  >>> {model}: cache HIT on call #2 ({ct} tokens) — supports caching")
            elif ct and ct == prev_cached:
                print(f"  >>> {model}: cached_tokens stable but small ({ct}) — likely noise, not real cache")
            else:
                print(f"  >>> {model}: no meaningful cache hit")
        prev_cached = ct
        await asyncio.sleep(0.5)


async def main():
    cfg, _ = load_config("config.json", "glm-4.6v")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    print(f"base_url: {cfg.base_url}")
    print(f"prefix: {len(PREFIX)} chars")

    for model in ["glm-4.6v", "glm-4.7", "glm-5.2"]:
        await probe(client, model)
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
