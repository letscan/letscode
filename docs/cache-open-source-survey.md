# Open-Source Agent Cache Adaptation Survey

**Date:** 2026-07-05
**Scope:** how 5 open-source AI coding agents adapt to provider-specific prompt
caching requirements (esp. Qwen/DashScope's explicit `cache_control` markers
vs automatic-caching providers like DeepSeek). Companion to
`docs/cache-probe-2026-07-05.md` (our first-hand verification).

Findings here come from direct source reads of `main` branches. Two
caveats from the researcher worth flagging:

1. **PiAgent / OpenClaw / HermesAgent star counts** returned implausibly high
   figures — they may be mirrors/forks. Code is internally consistent but
   provenance is less certain than OpenCode/Cline/Zed.
2. Where a finding reads "verified", the agent pulled actual source from
   `raw.githubusercontent.com`; where uncertain it's marked.

---

## TL;DR — the cross-agent pattern

All five agents converge on the **same three-layer design**, which is exactly
the shape letscode needs:

| Layer | Responsibility | letscode mapping |
|---|---|---|
| **Marker injection** | decide *whether* to add `cache_control` and *where* (system / tools / last message) | message-construction site in `agent.py` |
| **Provider routing** | per-provider opt-in: explicit markers (Anthropic/Qwen) vs `prompt_cache_key` (OpenAI/DeepSeek) | config-level `cache: "explicit"\|"auto"` flag per model |
| **Usage normalization** | unify `cached_tokens` / `cache_creation_input_tokens` / `prompt_cache_hit_tokens` into one shape | `stream.py:_process_chunk` (the gap we already identified) |

The big differentiator across agents is **how aggressively they opt non-Anthropic
providers into explicit markers** — HermesAgent > Cline > OpenCode > OpenClaw > PiAgent.

---

## Per-agent findings

### 1. OpenCode (`sst/opencode`) — single merged `providerOptions` blob

Cleanest design of the five. All cache logic lives in **one file**:
`packages/opencode/src/provider/transform.ts`.

**The trick:** `applyCaching()` emits *every* provider's marker key into one
merged blob, so the same code path serves all marker-based providers without
branching:

```ts
const providerOptions = {
  anthropic:        { cacheControl: { type: "ephemeral" } },
  openrouter:       { cacheControl: { type: "ephemeral" } },
  bedrock:          { cachePoint:   { type: "default" } },
  openaiCompatible: { cache_control: { type: "ephemeral" } },  // Qwen/DeepScope/DeepSeek
  copilot:          { copilot_cache_control: { type: "ephemeral" } },
  alibaba:          { cacheControl: { type: "ephemeral" } },    // native DashScope SDK
}
```

- **What gets marked:** first 2 system messages (stable prefix) + last 2
  non-system messages (rolling tail). Anthropic/Bedrock get message-level
  markers; all others (incl. Qwen) get last-content-part-level markers.
- **Gate:** `message()` only calls `applyCaching()` for Anthropic family +
  `@ai-sdk/alibaba` (native Qwen) — see lines ~432-446.
- **Auto-cache providers (OpenAI/DeepSeek):** no markers; instead `options()`
  sets a stable `promptCacheKey = sessionID`. So the split is "markers for
  Anthropic/Qwen, key for OpenAI-family".
- **Usage:** `session/llm/ai-sdk.ts:usage()` normalizes both
  `inputTokenDetails.cacheReadTokens` (AI SDK v5) **and** legacy
  `cachedInputTokens` (DeepSeek shape). ACP layer surfaces `cachedReadTokens`/
  `cachedWriteTokens`.

**Lesson for letscode:** one transform, multiple marker shapes in one dict —
avoids per-provider if/else explosion. letscode's equivalent would be a
`_cache_marker_for(model)` returning the right blob for the active provider.

### 2. Cline (`cline/cline`) — declarative routing table, last-user-message only

Recently refactored into a monorepo; cache logic moved to shared SDK at
`sdk/packages/llms/`. Uses the Vercel AI SDK `providerOptions` mechanism.

**The trick:** each builtin provider carries a `metadata.routing.promptCache`
descriptor. Routing matches by `anthropic-compatible` / `model-family` /
`model-id`, optionally gated on a `prompt-cache` capability.

- **Strategy:** marks the **last user message's last text part** (NOT last-N) +
  a top-level Anthropic breakpoint in the `anthropic` provider-options bucket.
  `buildCachedAiSdkMessages` walks back from the end, marks only the latest
  `role:"user"` turn, then breaks.
- **Qwen:** first-class citizen. Separate `QWEN_PROMPT_CACHE_ROUTE` matched by
  `family: "qwen"` AND gated on `prompt-cache` capability (see
  `anthropic-compatible.ts` lines 40-90). Two Qwen providers registered
  (`builtins.ts:780-804`), both pointing at DashScope's OpenAI-compat endpoint.
- **Same-shape, different bucket:** Anthropic → `anthropic` bucket; OpenAI-compat
  → `openaiCompatible` bucket with a trailing whitespace text block to keep the
  marker on a content part.
- **Usage cascade:** `normalizeUsage` (`ai-sdk.ts:670-812`) tries a long list of
  field names: `prompt_tokens_details.cached_tokens` (OpenAI),
  `cache_creation_input_tokens` (Anthropic), `cache_read_tokens`,
  `cachedContentTokenCount`, etc. Then applies cache-aware pricing (1.25× write
  premium default, separate cacheRead/cacheWrite rates).
- **UI:** `TaskHeader.tsx` shows cache read/write counts + cost; per-provider
  `usageCostDisplay: "hide"` suppresses for subscription providers.

**Lesson for letscode:** a *declarative* routing table per provider is more
maintainable than scattered conditionals. Cline's "last user message only"
strategy is simpler than OpenCode's "first 2 + last 2" and probably sufficient
for letscode's single-turn-per-agent-loop model.

### 3. Zed Native Agent (`zed-industries/zed`) — hybrid TTL strategy, the most sophisticated

Rust. Two-level design: agent crate sets a single `cache: bool`, provider
crates translate it.

**The trick:** a **hybrid TTL** strategy for Anthropic — long-TTL (1h) explicit
breakpoint on the static prefix (last tool + system) + automatic top-level
caching for the conversation tail.

```rust
// crates/anthropic/src/completion.rs
pub enum AnthropicPromptCacheMode { Disabled, Legacy, Automatic }
```

- **Agent side:** `crates/agent/src/thread.rs:build_request_messages_until`
  builds `[system, ...history]` then marks ONLY `last_message.cache = true`.
  The generic `LanguageModelRequestMessage.cache: bool` is the entire agent-
  layer contract.
- **Translator side:** `into_anthropic(...)` with `Automatic` mode marks
  `tools.last_mut()` and the system content's final text block with
  `CacheControl { Ephemeral, ttl: OneHour }`, plus sets top-level
  `Request.cache_control = Some(Ephemeral)` (5-min default, refreshes free on
  hit) for the conversation tail.
- **Provider selection:** native Anthropic + Zed AI Anthropic → `Automatic`;
  Anthropic-compatible → `Legacy` if `prompt_caching` capability, else
  `Disabled`; Bedrock → own `CachePoint` blocks; OpenAI → no markers,
  `prompt_cache_key = thread_id`.
- **Bonus:** `agent.rs:979` deliberately avoids re-rendering the system prompt
  when `ProjectContext` is unchanged — byte-identical system prompt preserves
  cache hits. **This is exactly the kind of cache-friendly invariant letscode
  should enforce.**
- **Usage:** `TokenUsage` struct with `cache_creation_input_tokens` /
  `cache_read_input_tokens`; parsed per provider (Anthropic direct, OpenAI
  `cached_tokens`, Bedrock `metadata.cache_*`). Telemetry emits on every usage
  update.

**Lesson for letscode:** the TTL-split idea (long TTL on stable prefix, short
TTL on conversation tail) is worth stealing if letscode ever supports Anthropic.
The "agent sets one bool, translator does the rest" separation is the cleanest
agent/provider boundary in the survey.

### 4. OpenClaw (`openclaw/openclaw`) — centralized policy, per-provider stream-wrappers

TypeScript monorepo. Policy concentrated in `src/agents/anthropic-payload-policy.ts`.

- **`resolveAnthropicEphemeralCacheControl`** returns `{type, ttl}` gated on
  hostname (`api.anthropic.com`, Vertex AI hosts). 1h TTL only on those hosts.
- **4-marker cap** (`ANTHROPIC_CACHE_CONTROL_LIMIT = 4`), subtracting markers
  already spent on system+tools.
- **Qwen:** explicit injection in `extensions/qwen/stream.ts` — sets
  `cache_control = {type:"ephemeral"}` on the last system content block.
- **DeepSeek:** `src/infra/provider-usage.fetch.deepseek.ts` has NO cache-token
  parsing — consistent with DeepSeek's automatic unreported caching.
- **OpenAI-compat:** `src/llm/providers/openai-prompt-cache.ts` clamps a
  64-char `prompt_cache_key`.
- **Unified usage:** `cacheRead` / `cacheWrite` fields in
  `src/infra/session-cost-usage.types.ts`.

**Caveat:** provenance uncertain (researcher flagged possible mirror/fork).

### 5. HermesAgent (`NousResearch/hermes-agent`) — most thorough Qwen handling

Python. The clearest example of a centralized policy function. **This is the
closest architectural match to letscode** (Python, similar shape).

- **`agent/prompt_caching.py`** (~110 lines): `apply_anthropic_cache_control`
  uses a `system_and_3` layout — marker on system prompt + last 3 non-system
  messages. Single shared TTL (5m or 1h). 4-breakpoint cap.
- **The brain:** `agent/agent_runtime_helpers.py :: anthropic_prompt_cache_policy()`
  returns `(should_cache, use_native_layout)`. Verified branches:
  - Native Anthropic → `(True, True)`
  - OpenRouter/Portal + Claude → `(True, False)` (envelope layout)
  - **Nous Portal + Qwen → `(True, False)`** — comment: *"without this branch…
    0% cache hits and re-billing the full prompt on every turn"*
  - **Qwen/Alibaba on OpenCode + direct DashScope → `(True, False)`** — comment:
    *"Without this branch qwen3.6-plus on opencode-go reports 0% cached tokens
    and burns through the subscription on every turn."* **This independently
    confirms our first-hand Qwen finding.**
  - **DeepSeek → `(False, False)`** — no markers, automatic server-side caching.
- **Usage:** `agent/usage_pricing.py :: normalize_usage()` unifies three shapes:
  Anthropic `cache_read_input_tokens`/`cache_creation_input_tokens`, Codex
  `input_tokens_details.cached_tokens`, OpenAI `prompt_tokens_details.cached_tokens`.
- **Smoking-gun comment** in `conversation_loop.py:2221`:
  > *"Surface cache hit stats for any provider that reports them — not just
  > those where we inject cache_control markers. OpenAI/Kimi/DeepSeek/Qwen all
  > do automatic server-side prefix caching and return
  > `prompt_tokens_details.cached_tokens`; users previously could not see their
  > cache % because this line was gated on `_use_prompt_caching`."*

**Lesson for letscode:** the HermesAgent policy function is almost a 1:1 template
for what letscode needs — a single `_prompt_cache_policy(model) -> (bool, layout)`
decision point, with explicit branches per provider family. The comment above
also confirms our finding that **usage display must be decoupled from marker
injection** (DeepSeek auto-caches even when we inject no markers).

### 6. PiAgent (`earendil-works/pi`) — data-driven per-model compat, with a Qwen gap

TypeScript monorepo. Per-model `compat.cacheControlFormat: "anthropic"` flag in
`*.models.ts` files drives behavior.

- **Qwen:** **partial / inconsistent** — `qwen3.6-plus` on OpenAI-wire has NO
  marker injection (relies on automatic + `cacheRead: 0.05` pricing), while
  `qwen3.7-max` on Anthropic-wire gets markers. This is a **genuine architectural
  difference** from OpenClaw/HermesAgent which uniformly inject Qwen markers.
- **DeepSeek:** `openai-completions`, no `cacheControlFormat`, `cacheWrite: 0`.
- **OpenAI direct:** uses `prompt_cache_key` (64-char clamp) +
  `prompt_cache_retention: "24h"` instead of `cache_control`.
- **Usage:** `parseChunkUsage` handles `prompt_tokens_details.cached_tokens`
  AND `prompt_cache_hit_tokens` (DeepSeek-native) in one read.

**Caveat:** provenance uncertain (researcher flagged possible mirror/fork).
**Lesson:** a data-driven per-model flag is maintainable, but only if you
actually set it for every provider that needs it — the Qwen-on-OpenAI-wire gap
is the cautionary tale.

---

## Synthesis — what letscode should take

Mapping the survey to letscode's concrete next steps (from
`docs/cache-probe-2026-07-05.md`):

### 1. Usage normalization (zero-risk first fix) — `stream.py`

Every surveyed agent unifies to a `(cacheRead, cacheWrite)` pair. letscode's
`_process_chunk` currently drops all cache fields. The normalized field list
across the survey:

```python
# Reads (cache hit):
"prompt_tokens_details.cached_tokens",       # OpenAI/Qwen/GLM
"input_tokens_details.cached_tokens",        # Codex Responses
"prompt_cache_hit_tokens",                   # DeepSeek-native
"cache_read_input_tokens",                   # Anthropic

# Writes (cache creation):
"prompt_tokens_details.cache_creation_input_tokens",
"cache_creation_input_tokens",               # Anthropic
"prompt_cache_miss_tokens",                  # DeepSeek-native (≈ creation)
```

HermesAgent's `normalize_usage` and Cline's `normalizeUsage` are the cleanest
templates.

### 2. Provider policy (the Qwen-shaped problem) — new helper

The consensus design is a **centralized policy function** returning
`(should_inject_markers, marker_layout)`. HermesAgent's
`anthropic_prompt_cache_policy()` is the closest template:

| Provider family | Inject markers? | Layout |
|---|---|---|
| Anthropic | yes | system + last-N messages |
| **Qwen / DashScope** | **yes** (explicit `cache_control` on content blocks) | system block |
| GLM (≥4.5/4.6) | no (automatic) | — |
| DeepSeek | no (automatic) | — |
| OpenAI | no (automatic, use `prompt_cache_key`) | — |

This maps cleanly onto letscode's existing per-model config (a `cache` field on
each model entry: `"explicit"` / `"auto"` / `"none"`).

### 3. Marker injection (the Qwen code change) — message construction

For Qwen specifically, letscode needs to:
- construct the system message `content` as a **content-block array** (not a
  bare string), and
- attach `"cache_control": {"type": "ephemeral"}` to the last block.

OpenCode's "one merged blob for all providers" pattern is overkill for letscode
(single provider per call), but the "last content part gets the marker" detail
is universal and matters — DashScope requires the marker *on a content block*,
not on the message.

### 4. Cache-friendly invariants (steal from Zed)

Zed's "don't re-render the system prompt if nothing changed" comment
(`agent.rs:979`) is the cheapest cache win available and applies regardless of
provider. letscode should audit its own system-prompt construction
(`prompt.py`) for sources of per-turn drift (timestamps, random IDs, etc.) —
any byte change in the prefix busts the cache for every provider.

---

## Sources

- OpenCode: [`sst/opencode`](https://github.com/sst/opencode) — `packages/opencode/src/provider/transform.ts`
- Cline: [`cline/cline`](https://github.com/cline/cline) — `sdk/packages/llms/src/providers/routing/anthropic-compatible.ts`, `ai-sdk.ts`, `builtins.ts`
- Zed: [`zed-industries/zed`](https://github.com/zed-industries/zed) — `crates/anthropic/src/completion.rs`, `crates/agent/src/thread.rs`, `crates/agent/src/agent.rs`
- OpenClaw: `openclaw/openclaw` (provenance uncertain) — `src/agents/anthropic-payload-policy.ts`, `extensions/qwen/stream.ts`
- HermesAgent: `NousResearch/hermes-agent` (provenance uncertain) — `agent/prompt_caching.py`, `agent/agent_runtime_helpers.py`, `agent/usage_pricing.py`
- PiAgent: `earendil-works/pi` (provenance uncertain) — `packages/ai/src/api/anthropic-messages.ts`, `openai-completions.ts`, `*.models.ts`
- Related discussions: [Cline #5067](https://github.com/cline/cline/issues/5067), [Cline #4346](https://github.com/cline/cline/issues/4346), [Zed Discussion #32372](https://github.com/zed-industries/zed/discussions/32372)
