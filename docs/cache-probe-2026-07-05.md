# LLM Provider Cache Support — Hands-on Verification

**Date:** 2026-07-05
**Scope:** verify which models in `config.json` return valid cached-token
statistics, and the activation requirements for each provider. This is the
first-hand baseline for the upcoming letscode cache-efficiency work.

## Test setup

Two reusable probes were used (`scripts/probe_cache.py` is kept as the
durable diagnostic; v2/repeat scripts were throwaway):

1. **`probe_cache.py`** — sends a ~5 KB low-entropy prefix + a short question
   to every model in `config.json`, twice back-to-back (call #2 should hit
   cache). Dumps the full `usage` dict for both streaming and non-streaming
   variants. Repeats the same prompt 4× on suspect providers to rule out
   cache-warmup latency.
2. **Round-2 probes** — tested (a) Qwen with explicit `cache_control` markers,
   (b) Qwen with implicit `x-dashscope-session-cache: enable` header,
   (c) GLM model-id sweep across `glm-4.6v-flash` / `glm-4.6` / `glm-4.5` /
   `glm-4-plus`.

All tests used `stream=True` + `include_usage` (matches letscode's actual
call path) and re-used each provider's key from `config.json`.

## Per-provider findings

### DeepSeek (`deepseek-v4-pro`, `deepseek-v4-flash`) — ✅ works out of the box

- **Activation:** automatic, no extra params/headers.
- **Returned fields:**
  - `prompt_tokens_details.cached_tokens` (OpenAI-style)
  - `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (DeepSeek-native,
    mutually consistent with `cached_tokens`)
- **Observed behavior** (prefix ~1740 tokens, ~1664 cacheable):
  - Call #1 stream: `cached_tokens=0` (cache cold; subsequent non-stream
    call in the same second already shows `1664` — warmup is fast)
  - Call #2 stream: `cached_tokens=1664`, `prompt_cache_miss_tokens=76`
  - Both v4-pro and v4-flash behave identically.
- **Coverage:** ~95% of the stable prefix is cacheable.

#### Cross-process multi-turn verification (2026-07-06)

A follow-up probe (`scripts/probe_ds_kv_cache.py`) verified DeepSeek's three
cache-prefix landing scenarios per the [official KV-cache doc](https://api-docs.deepseek.com/zh-cn/guides/kv_cache),
each call using a **fresh client** (mimicking letscode's `--feed` subprocess
model — same session, cross-process):

| Scenario | Doc claim | Measured | Verdict |
|---|---|---|---|
| **1. 请求结束位置落盘** (multi-turn: T2 = [S,U1,A1,U2]) | T2 hits T1's [S,U1] prefix unit | short prefix (30t): `hit=0`; **long prefix (1119t): `hit=1024`**; identical-request replay (608t): #2 `hit=512` | ✅ confirmed, **but with an undocumented minimum-length gate (~256 tokens)** |
| **2. 公共前缀检测落盘** (long-doc QA: T1/2/3 share [S,DOC], different Q) | system detects common prefix, T3 hits [S,DOC] | T1 `hit=0`, T2 `hit=1536`, T3 `hit=1536` | ✅ confirmed (T2 already hits — common-prefix detection is fast) |
| **3. 固定 token 间隔落盘** (single long input, ~1500t) | long inputs land units at fixed intervals | T1 `miss=1536`, T2 `hit=1408 miss=129` | ✅ confirmed (partial hit = interval units) |

**Key finding (not in the doc):** scenario 1 has a **minimum landing length**.
A 30-token prefix never lands even after a 5s wait; a ~1000-token prefix
lands within 3s. This explains the earlier letscode observation that looked
like "only system prompt gets cached": letscode's system prompt (~5632
tokens) is far above the gate, so it always lands; but each short Q&A turn
adds only tens of tokens of history, which individually fall below the gate
and don't land as independent units. Once accumulated history crosses the
gate, it does cache (scenarios 2/3 confirm long prefixes hit).

### Qwen / DashScope (`qwen3.5-plus-2026-04-20`) — ⚠️ requires explicit marker

Three regimes were tested:

| Approach | Result |
|---|---|
| **Bare request** (no header, no marker) | `cached_tokens = None` on every call. No signal at all. |
| **Implicit** — `x-dashscope-session-cache: enable` header only | `cached_tokens = None`, `cache_creation_input_tokens = None` on every call. Header alone does **not** populate usage. |
| **Explicit** — `cache_control: {type: "ephemeral"}` on the system content block | ✅ Works cleanly. Call #1: `cache_creation_input_tokens=1578`, `cached_tokens=0`. Call #2: `cached_tokens=1578`, `cache_creation=0`. |

**Conclusion:** DashScope only emits usable cache stats when you use the
**Anthropic-style explicit `cache_control` marker** on a content block.
Per the docs, implicit cache has a nondeterministic hit rate ("even if the
context is identical, it may still miss"), so for a deterministic, observable
optimization letscode must use explicit markers.

- **Pricing:** cache creation = 1.25× input; cache hit = 0.1× input.

### Zhipu GLM — ⚠️ automatic but model-gated

- **Activation:** none — Zhipu's docs say "automatic cache recognition, no
  manual config needed", and the probes confirm there is **no** extra body
  param or header to toggle. Confirmed not a missing-parameter issue.
- **The gate is model support.** Same key, same ~10 KB prefix, two back-to-back
  calls (`scripts/probe_glm_cache.py`). `cached_tokens` on call #2 tells the
  story — a real cache hit jumps from 0 to ~1500+; a non-supporting model
  returns a stable single-digit noise value that doesn't grow across calls:

  | Model | Call #1 `cached_tokens` | Call #2 `cached_tokens` | Verdict |
  |---|---|---|---|
  | `glm-4.6v-flash` | 0 | 0 | ❌ not supported |
  | `glm-4.6v` (vision) | 6 | 5 | ❌ not supported (single-digit noise, stable across calls) |
  | `glm-4.6` | 0 | **1536** | ✅ supported (95% hit) |
  | `glm-4.5` | 7 | **1590** | ✅ supported (~100% hit) |
  | `glm-4.7` | 0 → 1536¹ | **1536** | ✅ supported (95% hit) |
  | `glm-5.2` | 0 → 1600¹ | **1600** | ✅ supported (99% hit) |
  | `glm-4-plus` (legacy) | `None` | `None` | ❌ not supported |

  ¹ On the second probe run, call #1 already showed the cached value because
  the first run had warmed the cache — evidence the cache is real and
  persists across short intervals.

- **Where the line falls:** `glm-4.7` and `glm-5.2` (the newer 200K-context
  models) support caching, matching Zhipu's doc claim of "GLM-5.2/5.1/5
  series" support. The `glm-4.6v` vision variant and the older
  `glm-4.6v-flash`/`glm-4-plus` do not — `cached_tokens` stays at a
  single-digit constant regardless of prefix length, call repetition, or
  marker presence. This is a property of the model/deployment, not a letscode
  bug: `cache: auto` is correct for GLM, and switching to a supported model
  (`glm-4.6`/`glm-4.7`/`glm-5.2`) is what unlocks the cache.
- **Caveat on the "vision" framing:** the original version of this doc
  over-generalized to "vision variants don't support caching." That's not
  a claim Zhipu's docs make. What the data actually shows is that the
  specific `glm-4.6v` / `glm-4.6v-flash` models return noise-level
  `cached_tokens`; we have not tested newer vision models (e.g. a
  hypothetical `glm-4.7v`/`glm-5v`), so no generalization is warranted.


## Current letscode gap (independent of providers)

`letscode/stream.py:_process_chunk` (lines 191–196) only extracts
`prompt_tokens` / `completion_tokens` / `total_tokens` and **silently drops**
every cache-related field:

```python
if chunk.usage is not None:
    state["usage"] = {
        "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
    }
```

So even though DeepSeek returns hit/miss counts today, neither `call_llm`'s
log line nor the event stream surfaces them. This is the zero-risk first
fix required before any optimization can be measured.

## Recommendation summary

| Provider | Activation | letscode action needed | Verifiable? |
|---|---|---|---|
| DeepSeek | automatic | Just plumb cache fields through `stream.py` | ✅ yes, today |
| Qwen | explicit `cache_control` marker | Mark system prompt (+ stable tool-schema prefix) with `cache_control: ephemeral`; switch to content-block message construction | ✅ yes, after change |
| GLM | automatic, but model-gated | Use a supported model: `glm-4.6`/`glm-4.7`/`glm-5.2` all cache; `glm-4.6v`/`glm-4.6v-flash`/`glm-4-plus` do not (config decision, not code) | ⚠️ needs supported model |

## How to reproduce

```bash
# All-model scan (streaming + non-streaming usage dump)
CONFIG_PATH=config.json uv run python scripts/probe_cache.py
```

The round-2 probes (Qwen explicit cache, Qwen implicit header, GLM model
sweep) were inline throwaway scripts; their results are transcribed into the
tables above and the scripts were removed after verification.
