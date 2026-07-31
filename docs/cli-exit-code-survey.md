# CLI Exit-Code / Process Contract — Cross-Product Survey

**Date:** 2026-07-31
**Scope:** how four coding-agent CLIs signal three outcome classes (fully
successful / finished-but-mid-run-issues / fatal-interrupted) to a parent
process via exit code + stdout/stderr. This is the reference basis for
letscode's own CLI contract (the P1 fix), and for documenting the design
choice.

## TL;DR — the three outcome classes across all four

| Product | ① Fully successful | ② Finished but mid-run issue | ③ Fatal / interrupted |
|---|---|---|---|
| **Claude Code** | exit 0, answer→stdout | **Mostly exit 0**, issue surfaced *in stdout* (sub-agent API error, tool error, retried rate limit all folded into the conversation); **only `--max-turns` is non-zero** | 143 (SIGTERM) / 137 (SIGKILL) / 130 (SIGINT, convention) / 1 (general) |
| **Codex CLI** | exit 0, final msg→stdout | **exit 1** (turn `Failed`/`Interrupted`/`error_seen`); BUT a tool failure the model recovers from → still **exit 0** (issue #15536) | interrupt→**exit 1**; signal-killed→130 |
| **Cursor CLI** | exit 0, `result` event | **Not distinguished** (schema only has `success`); has bugs: sometimes exit 1 after success | non-zero + stderr, no terminal event; specific codes undocumented |
| **Pi Agent** | exit 0 | Mostly exit 0; API error exhausted → **exit 1 in text mode**, but **exit 0 in JSON mode** (footgun) | SIGTERM→143, SIGHUP→129, SIGINT→~130 (not trapped) |

## The two schools for outcome ② (the crux)

This is the load-bearing distinction for letscode's own design.

### School A — "soft failure" (Claude Code, Cursor): exit 0, problem described in stdout

- Sub-agent failure, tool failure, rate-limit retry: **none flip the exit code**.
  The failure text is returned *as content* (e.g. Claude's
  `Agent terminated early due to an API error: <detail>` becomes the sub-agent's
  result, fed back to the main agent).
- Philosophy: **the LLM can ingest failure info and self-adapt**, so the exit
  code shouldn't interrupt the caller. Diagnose by parsing stdout (especially
  the structured stream: Claude's `stream-json`, Cursor's `result` event).
- The one hard failure: `--max-turns` (explicitly non-zero).

### School B — "hard failure" (Codex): exit 1, signal the failure to the caller

- Turn final status `Failed`/`Interrupted` → exit 1. The caller perceives
  "this didn't complete normally" from the exit code alone.
- Caveat: a tool failure the model recovers from → exit 0 (community issue
  #15536 complains about this inconsistency).
- Philosophy: **the caller needs an explicit success/fail signal**, it doesn't
  trust the LLM's self-digestion. To sub-classify, parse `--json`'s
  `turn/completed` `status` field.

### letscode's decision: School A (soft failure)

Rationale: it matches letscode's existing tool-result philosophy — a failing
tool already returns `<error>...</error>` as a string to the LLM (see
`tools/runner.py`), it does **not** crash the run. Extending that to the CLI
contract keeps the model one step: a failed sub-agent's output becomes a
descriptive text on stdout (exit 0), so the parent (gs / Agent tool / DevHard)
feeds it back as data rather than treating it as a process-level exception.

The current letscode bug is the worst-of-both: on sub-agent cancellation it
produces **exit 0 + empty stdout** — neither School A (stdout should carry the
failure description) nor School B (exit should be non-zero). The P1 fix makes
it cleanly School A.

## Industry consensus (all four agree)

1. **stdout = payload only** (answer / result event); **stderr = diagnostics**.
   Universal hard rule.
2. **Signal exit codes follow POSIX**: `130 = SIGINT`, `137 = SIGKILL`,
   `143 = SIGTERM`. All four obey.
3. **Exit code alone is never granular enough** to distinguish *which* mid-run
   issue occurred — to sub-classify you must parse structured output (Claude
   `stream-json`, Codex `--json` status, Pi `stopReason`).
4. **Every product has exit-code inconsistencies/bugs** (Cursor exits 1 after
   success; Pi's JSON mode swallows errors; Codex recovers tool errors to
   exit 0). The contract is still evolving everywhere; there is no perfect
   reference implementation.

## Per-product detail

### Claude Code
- Sources: official docs ([cli-reference](https://code.claude.com/docs/en/cli-reference), [headless](https://code.claude.com/docs/en/headless), [errors](https://code.claude.com/docs/en/errors)).
- `-p`/`--print` is the subprocess mode. Output formats: `text`/`json`/`stream-json`.
- SIGTERM during `-p` → graceful → **exit 143** (officially documented).
- Sub-agent API failure → `Agent terminated early due to an API error: …` returned to main agent as the result; run can still **exit 0**.
- Incomplete response → prints last text block + `The response above may be incomplete` on stdout; treated as a turn ending, not a process error.
- `--max-turns` → "exits with an error" (non-zero); exact number undocumented.
- No published exit-code table; a `3/4/5` table floating on community sites is **not** from Anthropic.

### Codex CLI (`codex exec`)
- Sources: Rust source ([codex-rs/exec/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/exec/src/lib.rs)), [non-interactive-mode docs](https://learn.chatgpt.com/docs/non-interactive-mode).
- **Every** `process::exit(N)` uses `1`; no 2/130 constants in the exec crate.
- `error_seen` set on: non-retryable `Error` notification, turn `Failed`/`Interrupted`, server-handler error → `if error_seen { exit(1) }`.
- Recovered tool failure → exit 0 (issue [#15536](https://github.com/openai/codex/issues/15536)).
- `TurnStatus` = `completed | interrupted | failed` — only visible via `--json`, not exit code.

### Cursor CLI (`agent` / `cursor-agent`)
- Sources: official docs ([output-format](https://cursor.com/docs/cli/reference/output-format), [parameters](https://cursor.com/docs/cli/reference/parameters)); forum bug reports.
- Binary `agent` (primary) / `cursor-agent` (legacy); headless mode `-p`/`--print`.
- Documented contract: "0 = success, non-zero = failure, errors to stderr; on failure no well-formed JSON / no terminal result event."
- **No published exit-code table**, no documented mid-run-issue subtype (schema only shows `subtype: "success"`).
- Known exit-code bugs: hangs after output; exits 1 after success (model-slug bug); exits 1 on macOS CI (Keychain).
- Also ships an ACP transport (`agent acp`) with a `stopReason` field (values undocumented).

### Pi Agent (`pi`)
- Sources: source code ([print-mode.ts](https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/modes/print-mode.ts), [main.ts](https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/main.ts)); [pi.dev docs](https://pi.dev/docs/latest/usage).
- Modes: interactive (default TTY), `-p`/`--print` (text, single-shot), `--mode json`, `--mode rpc`.
- **Footgun**: in JSON mode a fatal API error (`stopReason === "error"|"aborted"`) still **exits 0** — the `exitCode = 1` assignment is inside an `if (mode === "text")` block. A harness must parse the final assistant message's `stopReason` from the event stream.
- Signals (print/json): SIGTERM→143, SIGHUP→129, SIGINT→~130 (not trapped, Node default).
- Startup/config errors → exit 1 with red stderr line.

## Implication for letscode (the P1 fix)

Goal: on sub-agent failure/cancellation, letscode CLI should behave per
**School A** — exit 0 (so callers don't treat it as a process exception) AND
emit a descriptive failure message on **stdout** (so the caller/LLM sees what
happened and can adapt). Concretely the "Interrupted, shutting down…" path
must, instead of dying with empty stdout, print something like
`Agent terminated early: <reason>` to stdout (mirroring Claude Code's
`Agent terminated early due to an API error: …`).

Signal-only deaths (SIGTERM/SIGKILL) keep their POSIX exit codes (143/137) —
those are genuinely fatal and not something stdout can describe.

## Sources (consolidated)
- Claude Code: [cli-reference](https://code.claude.com/docs/en/cli-reference), [headless](https://code.claude.com/docs/en/headless), [errors](https://code.claude.com/docs/en/errors)
- Codex: [exec/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/exec/src/lib.rs), [non-interactive-mode](https://learn.chatgpt.com/docs/non-interactive-mode), [issue #15536](https://github.com/openai/codex/issues/15536)
- Cursor: [output-format](https://cursor.com/docs/cli/reference/output-format), [parameters](https://cursor.com/docs/cli/reference/parameters), [ACP](https://cursor.com/docs/cli/acp)
- Pi: [print-mode.ts](https://raw.githubusercontent.com/earendil-works/pi/main/packages/coding-agent/src/modes/print-mode.ts), [pi.dev/usage](https://pi.dev/docs/latest/usage), [pi.dev/json](https://pi.dev/docs/latest/json)
