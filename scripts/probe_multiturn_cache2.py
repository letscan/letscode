"""Harsher multi-turn cache A/B test — longer history, fresh client, single pass.

The first probe found that once the Qwen backend cache warms, all strategies
converge to ~99% — making it hard to distinguish strategies. This probe:
  1. Uses a LONGER system prompt (~15 KB) so the cacheable prefix dominates.
  2. Builds a realistic 8-message history (4 turns) BEFORE the strategy test,
     so each strategy starts with substantial history.
  3. Runs each strategy ONCE, in isolation, with a sleep between to let the
     explicit-cache entry (5-min TTL) persist but reduce backend warming
     cross-contamination.
  4. Tests the hardest case: turn N+1 where the history grew by one big tool
     result. This is exactly where qwen-code's "last message only" strategy
     fails (Issue #5942).

Key metric: cache_creation on turns 2+ — non-zero creation means the strategy
FAILED to keep the prefix cached (had to rebuild). Lower creation = better.
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

# ~15 KB stable system prompt.
SYSTEM_PROMPT = (
    "You are a meticulous senior software engineer working on the letscode "
    "project — a lightweight Python AI agent harness. Memorize the full "
    "architecture guide below; later turns will reference specific sections.\n\n"
    "## Architecture\n"
) + (
    "The system is divided into a streaming core, a tool dispatcher, an event "
    "emitter, and a configuration layer. The streaming core parses SSE chunks "
    "and folds them into a single accumulator state. The tool dispatcher maps "
    "schema names to executor functions via a flat dictionary. The event "
    "emitter writes JSONL records with a stable schema. The configuration "
    "layer flattens provider/models into per-model dicts and merges secrets. "
    "The security layer applies glob rules, seatbelt sandboxes, and per-tool "
    "permission checks. The MCP integration discovers external tools and "
    "prefixes them to avoid collisions with built-ins. "
) * 35

MARKER = {"cache_control": {"type": "ephemeral"}}


def _promote(msg: dict) -> dict:
    c = msg.get("content")
    if isinstance(c, str):
        return {**msg, "content": [{"type": "text", "text": c}]}
    return msg


def _mark(msg: dict) -> dict:
    msg = _promote(msg)
    blocks = msg["content"]
    if blocks and isinstance(blocks[-1], dict) and not blocks[-1].get("cache_control"):
        blocks[-1] = {**blocks[-1], **MARKER}
    return msg


def apply_strategy(messages: list[dict], strategy: str) -> list[dict]:
    msgs = [dict(m) for m in messages]
    if strategy == "none":
        return msgs
    if strategy == "system_only":
        msgs[0] = _mark(msgs[0])
        return msgs
    if strategy == "system_plus_last":
        msgs[0] = _mark(msgs[0])
        if len(msgs) > 1:
            msgs[-1] = _mark(msgs[-1])
        return msgs
    if strategy == "system_plus_rolling":
        msgs[0] = _mark(msgs[0])
        non_sys = [i for i, m in enumerate(msgs) if m.get("role") != "system"]
        if len(non_sys) >= 2:
            msgs[non_sys[-2]] = _mark(msgs[non_sys[-2]])
            msgs[non_sys[-1]] = _mark(msgs[non_sys[-1]])
        elif len(non_sys) == 1:
            msgs[non_sys[-1]] = _mark(msgs[non_sys[-1]])
        return msgs
    raise ValueError(strategy)


# A realistic 4-turn history with tool calls — mimics a letscode session
# that has already done some work. Each turn adds substantial content.
def seed_history() -> list[dict]:
    h = []
    # Turn 1
    h.append({"role": "user", "content":
        "I'm working on letscode. First, explain how the agent loop works."})
    h.append({"role": "assistant", "content":
        "The agent loop in run_agent() streams LLM responses, extracts tool calls, "
        "executes them via the tool dispatcher, and feeds results back until the "
        "LLM stops requesting tools. It maintains line-buffered output to avoid "
        "per-token flicker and accumulates tool call fragments by index from "
        "streaming chunks. All tool results pass through _process_tool_result(), "
        "a single processing step producing the canonical result for agent "
        "context, event log, and stdout."})
    # Turn 2 — with a tool call (Read)
    tc2 = {"id": "c2", "type": "function",
           "function": {"name": "Read", "arguments": '{"file_path":"letscode/stream.py"}'}}
    h.append({"role": "assistant", "content": None, "tool_calls": [tc2]})
    h.append({"role": "tool", "tool_call_id": "c2",
              "content":
              "1  \"\"\"Stream consumer — pure LLM stream parsing with zero side effects.\"\"\"\n"
              "2  import asyncio, sys, time\n"
              "3  from dataclasses import dataclass, field\n"
              "4  from openai import AsyncOpenAI, OpenAI\n"
              "...(lines 5-340 elided for brevity)...\n"
              "340      return _state_to_result(state)"})
    h.append({"role": "assistant", "content":
        "stream.py defines consume_stream_async() which calls _consume_stream_once_async. "
        "It uses stream_options include_usage to get token stats. The _process_chunk "
        "function folds each chunk into accumulator state."})
    # Turn 3 — another tool call (Grep)
    tc3 = {"id": "c3", "type": "function",
           "function": {"name": "Grep",
                        "arguments": '{"pattern":"cache_control","path":"letscode"}'}}
    h.append({"role": "assistant", "content": None, "tool_calls": [tc3]})
    h.append({"role": "tool", "tool_call_id": "c3",
              "content":
              "letscode/stream.py:191:    if chunk.usage is not None:\n"
              "letscode/stream.py:192:        state[\"usage\"] = _normalize_usage(chunk.usage)\n"
              "letscode/stream.py:212:            ptd.get(\"cached_tokens\")\n"
              "No cache_control injection found anywhere — only usage normalization."})
    h.append({"role": "assistant", "content":
        "Confirmed: letscode currently reads cache stats from usage but never injects "
        "cache_control markers into requests. This is the gap we're closing."})
    return h


async def call_once(client, model, max_tokens, messages, tag="") -> dict:
    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model=model, messages=messages,
        max_tokens=min(max_tokens, 60),
        stream=True, stream_options={"include_usage": True},
        extra_body={"enable_thinking": False},
    )
    u = None
    async for ch in resp:
        if ch.usage is not None:
            u = json.loads(ch.usage.model_dump_json())
    dt = time.monotonic() - t0
    ptd = (u or {}).get("prompt_tokens_details") or {}
    return {
        "tag": tag,
        "elapsed_s": round(dt, 2),
        "prompt_tokens": (u or {}).get("prompt_tokens", 0),
        "cached_tokens": ptd.get("cached_tokens"),
        "cache_creation": ptd.get("cache_creation_input_tokens"),
    }


def fmt(r: dict) -> str:
    ct = r.get("cached_tokens")
    cc = r.get("cache_creation")
    pt = r.get("prompt_tokens", 0)
    hit = f"{(ct or 0) * 100 // pt}%" if (ct and pt) else "—"
    return f"prompt={pt} cached={ct} creation={cc} hit={hit} ({r['elapsed_s']}s)"


async def run_strategy(client, model, max_tokens, strategy: str) -> None:
    print(f"\n{'=' * 72}\nSTRATEGY: {strategy}\n{'=' * 72}")
    history = seed_history()

    # Turn A: baseline — user follow-up (history is 11 msgs)
    msgs_a = [{"role": "system", "content": SYSTEM_PROMPT}] + history + \
             [{"role": "user", "content": "Given all that, what should we cache first?"}]
    r_a = await call_once(client, model, max_tokens, apply_strategy(msgs_a, strategy), "A baseline")
    print(f"  Turn A (seeded history, 1st req): {fmt(r_a)}")
    await asyncio.sleep(0.5)

    # Turn B: history grew by one big tool result (the hard case from #5942)
    tc_b = {"id": "cb", "type": "function",
            "function": {"name": "Read", "arguments": '{"file_path":"letscode/agent.py"}'}}
    history_b = history + [
        {"role": "assistant", "content": "Let me check the agent loop assembly point.", "tool_calls": [tc_b]},
        {"role": "tool", "tool_call_id": "cb",
         "content": "agent.py line 82-83:\n"
                    "  messages = [{\"role\": \"system\", \"content\": system_prompt}] + msg_sub.messages\n"
                    "agent.py line 89-93:\n"
                    "  stream_result = await consume_stream_async(\n"
                    "      client, config.model, messages, config.max_tokens, ...)\n"
                    "This is the single assembly site before the API call."},
        {"role": "assistant", "content": "The assembly point is agent.py:83 — that's where we inject markers."},
    ]
    msgs_b = [{"role": "system", "content": SYSTEM_PROMPT}] + history_b + \
             [{"role": "user", "content": "Now implement the cache marker injection at that point."}]
    r_b = await call_once(client, model, max_tokens, apply_strategy(msgs_b, strategy), "B grew+tool")
    print(f"  Turn B (history grew, big tool):  {fmt(r_b)}")
    await asyncio.sleep(0.5)

    # Turn C: another follow-up, history grew again
    history_c = history_b + [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "cc", "type": "function",
                         "function": {"name": "Edit",
                                      "arguments": '{"file_path":"letscode/agent.py","old_string":"x","new_string":"y"}'}}]},
        {"role": "tool", "tool_call_id": "cc",
         "content": "The file letscode/agent.py has been updated."},
        {"role": "assistant", "content": "Done. The marker injection now runs at agent.py:84."},
    ]
    msgs_c = [{"role": "system", "content": SYSTEM_PROMPT}] + history_c + \
             [{"role": "user", "content": "What's the expected cache hit rate now?"}]
    r_c = await call_once(client, model, max_tokens, apply_strategy(msgs_c, strategy), "C grew again")
    print(f"  Turn C (history grew again):      {fmt(r_c)}")


async def main():
    cfg, _ = load_config("config.json", "qwen3.5-plus-2026-04-20")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    print(f"Model: {cfg.model}")
    print(f"System prompt: {len(SYSTEM_PROMPT)} chars ({len(SYSTEM_PROMPT)//4} ~tokens)")

    for strategy in ["none", "system_only", "system_plus_last", "system_plus_rolling"]:
        try:
            await run_strategy(client, cfg.model, cfg.max_tokens, strategy)
        except Exception as e:
            print(f"\n{strategy}: ERROR {type(e).__name__}: {str(e)[:200]}")
        await asyncio.sleep(2.0)  # let cache TTL settle between strategies


if __name__ == "__main__":
    asyncio.run(main())
