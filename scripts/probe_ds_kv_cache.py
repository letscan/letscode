"""Verify DeepSeek's 3 cache-prefix landing scenarios (per official doc:
https://api-docs.deepseek.com/zh-cn/guides/kv_cache).

All scenarios simulate letscode's real multi-turn path: each request is a
SEPARATE client/process call (matching letscode's --feed subprocess model),
with sleep between calls to allow the seconds-level disk-cache write.

Scenario 1 — 请求结束位置落盘:
  Turn 1 = [S, U1]; Turn 2 = [S, U1, A1, U2].
  Per doc example 1, Turn 2 should fully match Turn 1's "user input end"
  prefix unit → cache hit on the [S, U1] part.

Scenario 2 — 公共前缀检测落盘:
  Turn 1 = [S, DOC, Q1]; Turn 2 = [S, DOC, Q2]; Turn 3 = [S, DOC, Q3].
  Per doc example 2, Turn 1/2 miss (different suffixes Q1/Q2), but the
  system detects common prefix [S, DOC] and lands it as a unit. Turn 3
  then hits [S, DOC]. We verify Turn 1→2→3 hit pattern.

Scenario 3 — 按固定 token 间隔落盘:
  A single long user input (>> interval threshold). After Turn 1 writes
  it, Turn 2 (identical long input + new question) should hit the portion
  that landed at fixed intervals, NOT just the end.

For each call we read the RAW usage fields the doc names:
  prompt_cache_hit_tokens / prompt_cache_miss_tokens.
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

# Let's keep system prompts SHORT and distinct per scenario so we can read
# exactly which prefix landed. ~30 tokens is enough to be its own unit.

SYS1 = "你是一位乐于助人的助手。" * 3  # scenario 1: short, matches doc example
SYS2 = "你是一位资深的财报分析师，擅长从财报中提取关键信息。" * 3  # scenario 2

# A "document" long enough that the fixed-interval landing (scenario 3) can
# produce multiple interval units within it. ~3000 chars ≈ 1000+ tokens.
LONG_DOC = (
    "2024年度财报摘要：\n"
    "营业收入同比增长15%，达到120亿元。主营业务成本80亿元，毛利率33%。"
    "研发投入15亿元，占营收12.5%。净利润18亿元，同比增长20%。"
    "现金流方面，经营性现金流净额22亿元，投资活动流出10亿元。"
    "资产负债率45%，流动比率2.1。应收账款周转天数45天，存货周转天数60天。"
) * 20  # repeat to make it long


def new_client(cfg) -> AsyncOpenAI:
    """Fresh client per call, mimicking letscode's per-subprocess model."""
    return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)


async def call(cfg, model, messages, label, *, max_tokens=30):
    """One call with a FRESH client. Returns (hit, miss, total_prompt)."""
    client = new_client(cfg)
    t0 = time.monotonic()
    r = await client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens,
        stream=True, stream_options={"include_usage": True},
    )
    usage = None
    async for ch in r:
        if ch.usage is not None:
            usage = ch.usage.model_dump()
    dt = time.monotonic() - t0
    hit = (usage or {}).get("prompt_cache_hit_tokens")
    miss = (usage or {}).get("prompt_cache_miss_tokens")
    pt = (usage or {}).get("prompt_tokens", 0)
    print(f"  [{label}] ({dt:.2f}s) prompt={pt} "
          f"hit={hit} miss={miss}")
    return hit, miss, pt


async def scenario1(cfg, model):
    """请求结束位置落盘 — doc example 1 (multi-turn)."""
    print(f"\n{'='*70}")
    print("场景1: 请求结束位置落盘(多轮对话,文档例一)")
    print(f"{'='*70}")
    print("  Turn1=[S,U1] → Turn2=[S,U1,A1,U2],Turn2 应命中 [S,U1] 段\n")

    # Turn 1
    msgs1 = [
        {"role": "system", "content": SYS1},
        {"role": "user", "content": "中国的首都是哪里？"},
    ]
    await call(cfg, model, msgs1, "Turn1 [S,U1]")
    # Doc says cache build is "秒级" — wait for it to land.
    print("  ...等待缓存落盘(3s)...")
    await asyncio.sleep(3)

    # Turn 2: prefix [S, U1, A1] is identical; new U2 at the end.
    msgs2 = [
        {"role": "system", "content": SYS1},
        {"role": "user", "content": "中国的首都是哪里？"},
        {"role": "assistant", "content": "中国的首都是北京。"},
        {"role": "user", "content": "美国的首都是哪里？"},
    ]
    hit, miss, pt = await call(cfg, model, msgs2, "Turn2 [S,U1,A1,U2]")
    print(f"\n  判定: Turn2 hit={hit}。若 hit ≈ [S,U1] 的 token 数 → 场景1确认")


async def scenario2(cfg, model):
    """公共前缀检测落盘 — doc example 2 (long-doc QA)."""
    print(f"\n{'='*70}")
    print("场景2: 公共前缀检测落盘(长文档问答,文档例二)")
    print(f"{'='*70}")
    print("  Turn1=[S,DOC,Q1] Turn2=[S,DOC,Q2] Turn3=[S,DOC,Q3]")
    print("  预期: T1/T2 miss, 系统检测公共前缀 [S,DOC] 落盘, T3 命中 [S,DOC]\n")

    base = [
        {"role": "system", "content": SYS2},
        {"role": "user", "content": LONG_DOC + "\n\n%s"},
    ]
    # T1
    msgs1 = [{"role":"system","content":SYS2},
             {"role":"user","content": LONG_DOC + "\n\n请总结一下这份财报的关键信息。"}]
    await call(cfg, model, msgs1, "Turn1 Q1=总结")
    await asyncio.sleep(3)
    # T2 — same [S,DOC], different question
    msgs2 = [{"role":"system","content":SYS2},
             {"role":"user","content": LONG_DOC + "\n\n请分析一下这份财报的盈利情况。"}]
    hit2, _, _ = await call(cfg, model, msgs2, "Turn2 Q2=盈利")
    await asyncio.sleep(3)
    # T3 — third question, should now hit the [S,DOC] common prefix
    msgs3 = [{"role":"system","content":SYS2},
             {"role":"user","content": LONG_DOC + "\n\n请分析一下公司收入与支出占比。"}]
    hit3, _, _ = await call(cfg, model, msgs3, "Turn3 Q3=收支占比")
    print(f"\n  判定: T2 hit={hit2}, T3 hit={hit3}。"
          f"若 T2≤T3 且 T3 显著命中 [S,DOC] 段 → 场景2确认")


async def scenario3(cfg, model):
    """按固定 token 间隔落盘 — single long input."""
    print(f"\n{'='*70}")
    print("场景3: 按固定 token 间隔落盘(长输入)")
    print(f"{'='*70}")
    print("  一次极长 user 输入,落盘后第二次相同输入应命中部分(间隔单元)\n")

    # Very long single user turn. No system to isolate the effect.
    long_input = "请仔细阅读以下内容并记住: " + ("架构设计文档。系统分为流式核心、工具分发、事件发射、配置层。" * 80)
    msgs1 = [{"role": "user", "content": long_input + "\n\n一句话总结。"}]
    print(f"  输入长度: {len(long_input)} chars")
    await call(cfg, model, msgs1, "Turn1 长输入(冷)")
    await asyncio.sleep(3)
    # Turn 2: identical prefix, slightly different tail question.
    msgs2 = [{"role": "user", "content": long_input + "\n\n两句话总结。"}]
    hit2, miss2, pt2 = await call(cfg, model, msgs2, "Turn2 相同前缀+新问题")
    print(f"\n  判定: Turn2 hit={hit2}。若 hit > 0 且 < 全部输入 → 间隔落盘生效")


async def main():
    cfg, _ = load_config("config.json", "deepseek-v4-flash")
    print(f"MODEL: {cfg.model}  base: {cfg.base_url}")
    print("每个请求用独立 client(模拟 letscode 跨 subprocess)")

    await scenario1(cfg, cfg.model)
    await asyncio.sleep(2)
    await scenario2(cfg, cfg.model)
    await asyncio.sleep(2)
    await scenario3(cfg, cfg.model)


if __name__ == "__main__":
    asyncio.run(main())
