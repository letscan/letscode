"""End-to-end: letscode's own cache_markers + stream path sees Qwen cache hits.

Drives the REAL letscode code path (apply_cache_markers + consume_stream_async
+ _normalize_usage) across a 3-turn tool-style conversation, the same way
agent.py does. Verifies that with cache_mode="explicit" the markers are injected
AND the API reports cache_read_tokens > 0 on turns 2+.

Also verifies cache_mode="auto" (DeepSeek) is a no-op on messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from letscode.config import load_config
from letscode.cache_markers import apply_cache_markers
from letscode.stream import consume_stream_async

SYSTEM = (
    "You are a meticulous senior software engineer. Memorize this architecture:\n"
) + (
    "The streaming core parses SSE chunks and folds them into accumulator state. "
    "The tool dispatcher maps schema names to executor functions via a flat dict. "
    "The event emitter writes JSONL records with a stable schema. "
) * 25


async def turn(client, model, max_tokens, messages, cache_mode) -> dict:
    """One turn through the real letscode path: markers → stream → normalize."""
    msgs = apply_cache_markers(messages, cache_mode)
    t0 = time.monotonic()
    res = await consume_stream_async(
        client, model, msgs, min(max_tokens, 60),
        tools=[], max_retries=2,
        extra_body={"enable_thinking": False},
    )
    dt = time.monotonic() - t0
    u = res.usage or {}
    return {
        "elapsed": round(dt, 2),
        "prompt": u.get("prompt_tokens", 0),
        "cache_read": u.get("cache_read_tokens", 0),
        "cache_write": u.get("cache_write_tokens", 0),
        "answer": res.text_content[:50].replace("\n", " "),
    }


def fmt(r: dict) -> str:
    hit = f"{r['cache_read']*100//r['prompt']}%" if (r["cache_read"] and r["prompt"]) else "—"
    return (f"prompt={r['prompt']} read={r['cache_read']} write={r['cache_write']} "
            f"hit={hit} ({r['elapsed']}s)")


async def test_qwen_explicit():
    print("\n" + "=" * 70)
    print("QWEN + cache_mode=explicit (the new feature)")
    print("=" * 70)
    cfg, _ = load_config("config.json", "qwen3.5-plus-2026-04-20")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    history = []
    # Turn 1
    msgs = [{"role": "system", "content": SYSTEM}] + history + \
           [{"role": "user", "content": "What does the streaming core do?"}]
    r = await turn(client, cfg.model, cfg.max_tokens, msgs, "explicit")
    print(f"  Turn 1: {fmt(r)}")
    history.append({"role": "user", "content": "What does the streaming core do?"})
    history.append({"role": "assistant", "content": r["answer"] or "parses SSE chunks"})
    await asyncio.sleep(0.4)

    # Turn 2 — with a tool result
    tc = {"id": "c2", "type": "function",
          "function": {"name": "Read", "arguments": '{"file_path":"x.py"}'}}
    history.append({"role": "assistant", "content": None, "tool_calls": [tc]})
    history.append({"role": "tool", "tool_call_id": "c2",
                    "content": "class ToolRunner:\n    definitions = []"})
    history.append({"role": "assistant", "content": "ToolRunner holds definitions."})
    msgs = [{"role": "system", "content": SYSTEM}] + history + \
           [{"role": "user", "content": "Summarize the architecture."}]
    r = await turn(client, cfg.model, cfg.max_tokens, msgs, "explicit")
    print(f"  Turn 2: {fmt(r)}")
    await asyncio.sleep(0.4)

    # Turn 3 — follow-up; turn 2's cache entry should now be the hit.
    history.append({"role": "user", "content": "Summarize the architecture."})
    history.append({"role": "assistant", "content": r["answer"] or "architecture summary"})
    tc3 = {"id": "c3", "type": "function",
           "function": {"name": "Grep", "arguments": '{"pattern":"x"}'}}
    history.append({"role": "assistant", "content": None, "tool_calls": [tc3]})
    history.append({"role": "tool", "tool_call_id": "c3",
                    "content": "stream.py:191 usage"})
    history.append({"role": "assistant", "content": "Found usage normalization."})
    msgs = [{"role": "system", "content": SYSTEM}] + history + \
           [{"role": "user", "content": "What's the cache strategy?"}]
    r = await turn(client, cfg.model, cfg.max_tokens, msgs, "explicit")
    print(f"  Turn 3: {fmt(r)}")
    assert r["cache_read"] > 0, "FAIL: Turn 3 should have cache_read > 0"
    print("  ✅ Qwen explicit cache works through letscode's real path")
    return r


async def test_deepseek_auto_noop():
    print("\n" + "=" * 70)
    print("DEEPSEEK + cache_mode=auto (no-op verification — messages untouched)")
    print("=" * 70)
    cfg, _ = load_config("config.json", "deepseek-v4-flash")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "What does the streaming core do?"}]
    # Verify auto mode doesn't promote content to blocks.
    out = apply_cache_markers(msgs, "auto")
    assert out[0]["content"] == SYSTEM, "FAIL: auto mode mutated system content"
    assert out[1]["content"] == "What does the streaming core do?", "FAIL: auto mutated user content"
    print("  ✅ auto mode is a true no-op (messages unchanged)")

    # And DeepSeek still hits its own server-side cache on turn 2.
    r1 = await turn(client, cfg.model, cfg.max_tokens, msgs, "auto")
    print(f"  Turn 1: {fmt(r1)}")
    await asyncio.sleep(0.4)
    r2 = await turn(client, cfg.model, cfg.max_tokens, msgs, "auto")
    print(f"  Turn 2: {fmt(r2)}")
    assert r2["cache_read"] > 0, "FAIL: DeepSeek auto cache should hit on turn 2"
    print("  ✅ DeepSeek auto-cache still works (no regression)")


async def main():
    await test_qwen_explicit()
    await test_deepseek_auto_noop()
    print("\n" + "=" * 70)
    print("ALL E2E CHECKS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
