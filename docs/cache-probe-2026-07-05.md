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
- **The gate is model support.** Same key, same prefix, same call:

  | Model | Call #1 `cached_tokens` | Call #2 `cached_tokens` | Verdict |
  |---|---|---|---|
  | `glm-4.6v-flash` (current `vision_model`) | 0 | 0 | ❌ not supported |
  | `glm-4.6` | 0 | **1536** | ✅ supported |
  | `glm-4.5` | 7 | **1590** (~100% hit) | ✅ supported |
  | `glm-4-plus` (legacy) | `None` | `None` | ❌ not supported |

- **Conclusion:** the current `glm-4.6v-flash` in `config.json` is too old
  to participate in caching. Switching the `vision_model` to `glm-4.6` or
  newer (`glm-4.5` hits ~100%) is what unlocks the cache — not any code
  change on letscode's side.

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
| GLM | automatic, but model-gated | Swap `glm-4.6v-flash` → `glm-4.6`/`glm-4.5` (config decision, not code) | ⚠️ needs model swap |

## How to reproduce

```bash
# All-model scan (streaming + non-streaming usage dump)
CONFIG_PATH=config.json uv run python scripts/probe_cache.py
```

The round-2 probes (Qwen explicit cache, Qwen implicit header, GLM model
sweep) were inline throwaway scripts; their results are transcribed into the
tables above and the scripts were removed after verification.
