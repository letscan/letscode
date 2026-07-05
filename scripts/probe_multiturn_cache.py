"""Multi-turn cache strategy A/B test against real Qwen/DashScope API.

Tests the core hypothesis BEFORE we write code: does breakpoint placement
on the rolling conversation history actually matter for hit rate?

Compares 4 strategies across 3 conversation turns with the SAME growing
history (mimicking letscode's agent loop):
  (A) NO markers   — baseline, should give cached_tokens=None (per our probe)
  (B) system only  — single marker on system content block
  (C) system + LAST message  — the qwen-code strategy (Issue #5942 defect)
  (D) system + 2nd-to-last + LAST  — the Claude Code rolling strategy

The conversation is tool-driven (mimicking letscode: user→assistant(tool_call)
→tool(result)→assistant(answer)), so message shapes match reality.

Reports cache_read_tokens / cache_creation_input_tokens per turn so we can see
exactly which strategy wins on multi-turn hit rate.
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

# A long, stable system prompt — the prime cache candidate. ~5 KB.
SYSTEM_PROMPT = (
    "You are a meticulous senior software engineer working on the letscode "
    "project — a lightweight Python AI agent harness. You have memorized the "
    "full architecture guide below and will refer back to it.\n\n"
    "## Architecture\n"
) + (
    "The system is divided into a streaming core, a tool dispatcher, an event "
    "emitter, and a configuration layer. The streaming core parses SSE chunks "
    "and folds them into a single accumulator state. The tool dispatcher maps "
    "schema names to executor functions via a flat dictionary. The event "
    "emitter writes JSONL records with a stable schema. The configuration "
    "layer flattens provider/models into per-model dicts and merges secrets. "
) * 25

MARKER = {"cache_control": {"type": "ephemeral"}}


# ---- marker strategies -----------------------------------------------------

def _promote_to_blocks(msg: dict) -> dict:
    """Return a copy of msg with string content promoted to a text block list.
    Idempotent: if content is already a list, returns msg unchanged."""
    c = msg.get("content")
    if isinstance(c, str):
        return {**msg, "content": [{"type": "text", "text": c}]}
    return msg


def _add_marker_to_last_block(msg: dict) -> dict:
    """Append cache_control to the last content block. Promotes string→blocks
    first if needed. Idempotent: skips if last block already has cache_control."""
    msg = _promote_to_blocks(msg)
    blocks = msg["content"]
    if blocks and isinstance(blocks[-1], dict):
        if blocks[-1].get("cache_control"):
            return msg  # already marked
        blocks[-1] = {**blocks[-1], **MARKER}
    return msg


def apply_strategy(messages: list[dict], strategy: str) -> list[dict]:
    """Return a NEW message list with markers applied per the chosen strategy.
    Original messages are not mutated (we deep-copy via dict spreads)."""
    msgs = [dict(m) for m in messages]

    if strategy == "none":
        return msgs

    if strategy == "system_only":
        msgs[0] = _add_marker_to_last_block(msgs[0])
        return msgs

    if strategy == "system_plus_last":
        msgs[0] = _add_marker_to_last_block(msgs[0])
        if len(msgs) > 1:
            msgs[-1] = _add_marker_to_last_block(msgs[-1])
        return msgs

    if strategy == "system_plus_rolling":
        msgs[0] = _add_marker_to_last_block(msgs[0])
        # Mark the 2nd-to-last AND last non-system messages (rolling window).
        non_sys = [i for i, m in enumerate(msgs) if m.get("role") != "system"]
        if len(non_sys) >= 2:
            msgs[non_sys[-2]] = _add_marker_to_last_block(msgs[non_sys[-2]])
            msgs[non_sys[-1]] = _add_marker_to_last_block(msgs[non_sys[-1]])
        elif len(non_sys) == 1:
            msgs[non_sys[-1]] = _add_marker_to_last_block(msgs[non_sys[-1]])
        return msgs

    raise ValueError(f"unknown strategy {strategy!r}")


# ---- conversation scaffolding ----------------------------------------------

def build_turn_messages(history: list[dict], user_text: str,
                        tool_call_id: str | None = None,
                        tool_result: str | None = None,
                        assistant_tool_call: dict | None = None) -> list[dict]:
    """Build the messages list for one turn, given the accumulated history.

    Mimics letscode's agent loop shape:
      [system, ...history, (optional tool call/result), user]
    """
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(history)

    if assistant_tool_call is not None:
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [assistant_tool_call],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_result or "",
        })
    else:
        msgs.append({"role": "user", "content": user_text})

    return msgs


async def call_once(client, model, max_tokens, messages) -> dict:
    """One streaming completion. Returns normalized usage dict."""
    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model=model, messages=messages,
        max_tokens=min(max_tokens, 80),
        stream=True, stream_options={"include_usage": True},
        extra_body={"enable_thinking": False},
    )
    u = None
    text = ""
    async for ch in resp:
        if ch.usage is not None:
            u = json.loads(ch.usage.model_dump_json())
        if ch.choices and ch.choices[0].delta.content:
            text += ch.choices[0].delta.content
    dt = time.monotonic() - t0
    ptd = (u or {}).get("prompt_tokens_details") or {}
    return {
        "elapsed_s": round(dt, 2),
        "prompt_tokens": (u or {}).get("prompt_tokens", 0),
        "cached_tokens": ptd.get("cached_tokens"),
        "cache_creation": ptd.get("cache_creation_input_tokens"),
        "answer_preview": text[:60].replace("\n", " "),
    }


async def run_strategy(client, model, max_tokens, strategy: str) -> list[dict]:
    print(f"\n{'=' * 72}")
    print(f"STRATEGY: {strategy}")
    print(f"{'=' * 72}")

    history: list[dict] = []
    results = []

    # Turn 1: plain user question
    msgs = build_turn_messages(history, "In one sentence, what does the streaming core do?")
    msgs = apply_strategy(msgs, strategy)
    r = await call_once(client, model, max_tokens, msgs)
    print(f"  Turn 1 (user Q):     {r}")
    results.append(r)
    history.append({"role": "user", "content": "In one sentence, what does the streaming core do?"})
    history.append({"role": "assistant", "content": "The streaming core parses SSE chunks and folds them into accumulator state."})
    await asyncio.sleep(0.4)

    # Turn 2: with a tool call (mimics letscode agent loop)
    tc = {
        "id": "call_turn2", "type": "function",
        "function": {"name": "Read", "arguments": '{"file_path":"/tmp/x.py"}'},
    }
    msgs = build_turn_messages(
        history, "Now read /tmp/x.py to check the tool dispatcher.",
        tool_call_id="call_turn2",
        tool_result="Contents of /tmp/x.py:\nclass ToolRunner:\n    definitions = []",
        assistant_tool_call=tc,
    )
    msgs = apply_strategy(msgs, strategy)
    r = await call_once(client, model, max_tokens, msgs)
    print(f"  Turn 2 (tool call):  {r}")
    results.append(r)
    history.append({"role": "assistant", "content": None, "tool_calls": [tc]})
    history.append({"role": "tool", "tool_call_id": "call_turn2",
                    "content": "Contents of /tmp/x.py:\nclass ToolRunner:\n    definitions = []"})
    history.append({"role": "assistant", "content": "The ToolRunner holds tool definitions in a flat list."})
    await asyncio.sleep(0.4)

    # Turn 3: follow-up (longer history now — cache should really pay off)
    msgs = build_turn_messages(history, "Given all that, summarize the architecture in one line.")
    msgs = apply_strategy(msgs, strategy)
    r = await call_once(client, model, max_tokens, msgs)
    print(f"  Turn 3 (follow-up):  {r}")
    results.append(r)

    return results


async def main():
    cfg, _ = load_config("config.json", "qwen3.5-plus-2026-04-20")
    client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
    print(f"Model: {cfg.model}  base: {cfg.base_url}")
    print(f"System prompt: {len(SYSTEM_PROMPT)} chars")

    all_results = {}
    for strategy in ["none", "system_only", "system_plus_last", "system_plus_rolling"]:
        try:
            all_results[strategy] = await run_strategy(client, cfg.model, cfg.max_tokens, strategy)
        except Exception as e:
            print(f"\n{strategy}: ERROR {type(e).__name__}: {str(e)[:200]}")
            all_results[strategy] = []
        await asyncio.sleep(1.0)

    # Summary table
    print(f"\n{'=' * 72}")
    print("SUMMARY — cached_tokens / cache_creation per turn")
    print(f"{'=' * 72}")
    print(f"{'Strategy':<24} {'Turn 1':<20} {'Turn 2':<20} {'Turn 3':<20}")
    for strategy, rs in all_results.items():
        cells = []
        for r in rs:
            ct = r.get("cached_tokens")
            cc = r.get("cache_creation")
            pt = r.get("prompt_tokens", 0)
            hit_pct = f"{(ct or 0) * 100 // pt}%" if (ct and pt) else "—"
            cells.append(f"{ct}/{cc} ({hit_pct})" if (ct or cc) else "None")
        while len(cells) < 3:
            cells.append("(err)")
        print(f"{strategy:<24} {cells[0]:<20} {cells[1]:<20} {cells[2]:<20}")


if __name__ == "__main__":
    asyncio.run(main())
