# Multi-Turn Cache Strategy A/B Test — First-Hand Results

**Date:** 2026-07-06
**Scope:** empirically validate which `cache_control` breakpoint placement
strategy gives the best multi-turn cache performance on Qwen/DashScope,
**before** committing to an implementation. Companion to
`docs/cache-probe-2026-07-05.md` (single-turn verification) and
`docs/cache-open-source-survey.md` (how other agents do it).

## Why this probe was needed

The first probe (`cache-probe-2026-07-05.md`) only proved that Qwen needs
**some** `cache_control` marker. It didn't tell us *where* to place markers for
multi-turn conversations — the actual letscode use case. The open-source survey
found disagreement:

- **qwen-code**: system + last message only → Issue #5942 reports only 80-84%
  hit rate (a known defect)
- **Claude Code**: system + rolling 2nd-to-last + last → 100% hit rate

We needed first-hand data to choose, not a guess based on others' claims.

## Test setup

Two probes (both kept as reusable diagnostics):
- `scripts/probe_multiturn_cache.py` — short history (3 turns, ~2K prompt tokens)
- `scripts/probe_multiturn_cache2.py` — **long history** (4 seeded turns + 3 test
  turns, ~4.5K prompt tokens, with realistic tool_call/tool_result messages)

Both compare 4 strategies on the same growing conversation:

| Strategy | Markers placed on |
|---|---|
| `none` | nothing (baseline) |
| `system_only` | system content block only |
| `system_plus_last` | system + **last** message (qwen-code's strategy) |
| `system_plus_rolling` | system + **2nd-to-last** + last (Claude Code's strategy) |

## Results — the decisive signal is `cache_creation`, not `cached_tokens`

### Long-history test (`probe_multiturn_cache2.py`), first run (cache cold)

| Strategy | Turn A (seeded) | Turn B (+tool) | Turn C (+tool) |
|---|---|---|---|
| `none` | cached=None | cached=None | cached=None |
| `system_only` | cached=0, **creation=3964** | cached=3964, hit 86% | cached=3964, hit 84% |
| `system_plus_last` | hit 89%, **creation=443** | hit 86%, **creation=595** | hit 84%, **creation=692** |
| `system_plus_rolling` | hit 99%, **creation=0** | hit 99%, **creation=0** | hit 99%, **creation=0** |

### Key finding — `cache_creation` is the real cost metric

`cached_tokens` (hit rate) looks similar for the last two strategies on warm
runs, but **`cache_creation` reveals the truth**:

- **`system_plus_last`** has **non-zero, growing `cache_creation` on every turn**
  (443 → 595 → 692). Each turn, the moving last-message breakpoint forces the
  provider to **re-write part of the cache**. This is exactly the qwen-code
  Issue #5942 defect, reproduced first-hand: high hit rate on paper, but
  repeated cache rebuilds inflating cost (creation is billed at 1.25×).

- **`system_plus_rolling`** is the only strategy with **`cache_creation = 0`
  on every turn, in every run**. The 2nd-to-last breakpoint guarantees the
  previous turn's content is *already* in the cache when the next request
  arrives — no rebuild, pure hit. This matches Claude Code's measured 100%.

- **`system_only`** plateaus at ~84-86% hit because only the system prefix is
  cached; growing history is never cached. Fine for single-turn, bad for agents.

- **`none`** returns `cached_tokens = None` always — reconfirms markers are
  mandatory on DashScope.

### Stability across re-runs

A second run showed `system_only` and `system_plus_last` jumping to ~99% hit
(the backend's implicit cache had warmed). But `system_plus_rolling` was the
**only strategy that hit 99% with zero creation in both runs** — the
deterministic winner, not dependent on backend warming luck.

## Verdict

**Adopt `system_plus_rolling`** (Claude Code's strategy): markers on
1. the system message content block, and
2. the **2nd-to-last** non-system message, and
3. the **last** non-system message.

This gives ~99% hit rate with **zero cache rebuilds** across turns — the
theoretical optimum. It avoids the qwen-code defect (moving-endpoint rebuilds)
that we've now reproduced first-hand.

The probe also validates the marker-mechanics design:
- Markers must be on content **blocks** (string `content` must be promoted to
  `[{"type":"text","text":..., "cache_control":...}]`) — verified.
- The `_mark` helper is idempotent (re-marking a marked message is a no-op) —
  important for feed-replay robustness.
- Tool messages (`role:"tool"`) can carry markers via the same promotion —
  verified, this is how the rolling breakpoint lands on tool-result turns.

## How to reproduce

```bash
# Short-history quick check
uv run python scripts/probe_multiturn_cache.py

# Long-history decisive test (the one that separates strategies)
uv run python scripts/probe_multiturn_cache2.py
```

## Postscript: `prompt.py` audit (2026-07-06)

After implementing the marker injection, we audited `letscode/prompt.py` to
check whether the system prompt contains any **per-turn-changing state**
(timestamps, random ids, real-time values) that would silently bust the
cache prefix across turns within a session.

**Finding: nothing to fix.** The system prompt is byte-stable within a session:

- No timestamps, dates, random values, or real-time state. All apparent
  matches for "time"/"date"/"2026" are false positives (English prose like
  "time estimates", the model's version string `qwen3.5-plus-2026-04-20`).
- `build_system_prompt()` produces byte-identical output across calls in the
  same process (verified by direct comparison).
- `_env_section` reads `os.getcwd()` each call, but `cwd` does not change
  during a session, so it's stable across turns. `_is_git_repo()` is memoized
  in a module global (`_is_git_cache`).
- `_skills_section()` reads the skill registry once per call, but the registry
  doesn't change mid-session.

This is consistent with the A/B result: `system_plus_rolling` already hits the
theoretical optimum (99% hit, zero `cache_creation`) on the *current* prompt
structure. The only remaining cache opportunity is **cross-session / cross-cwd**
sharing (different projects share less prefix), but that's out of scope —
prompt caching only needs to win within a session, and it already does.

The Claude Code discipline of "inject volatile info into the next user message,
not the system prompt" is therefore **already satisfied** by letscode's
prompt construction, with no code change required.

