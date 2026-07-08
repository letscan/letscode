# AGENTS.md

This file provides guidance to AI coding agents working with code in this repository.

## Project Overview

letscode is a lightweight Python AI agent harness (v0.4.0) that implements a ReAct-pattern agent loop over OpenAI-compatible APIs. It provides an LLM → Tool Execution → Result Feedback cycle for autonomous software engineering tasks.

- **Language**: Python 3.11+ (managed with `uv`)
- **Core dependencies**: `openai>=1.0`, `mcp>=1.27.0`, `agent-client-protocol>=0.10.0`, `pyyaml>=6.0`
- **Default model**: GLM-5-Turbo via 智谱AI API

## Common Commands

```bash
# Install dependencies (uv required)
uv sync

# Create config from template
cp config.example.json config.json

# Run the agent
letscode "your prompt here"
python -m letscode "your prompt here"

# With options
letscode -c config.json -m glm-5-turbo -w /path/to/workspace -v "prompt"

# List available models from config
letscode --models [-c config.json]

# List available agent cards (built-in + project)
letscode --list-agents

# Run as a specific agent card (replaces system prompt, restricts tools/rules)
letscode --as Explore "find all async functions"
letscode --as Plan "plan a caching layer for config.py"

# Structured prompt input (ACP-compatible content blocks)
letscode --prompt-format json '[{"type":"text","text":"hello"}]'

# Event stream mode (JSONL to stdout)
letscode --event-stream "prompt"

# Multi-turn: continue from a previous session log
letscode --feed .letscode/logs/20260501_abcd.jsonl --append "follow-up prompt"

# Run ACP server (stdio protocol for IDE/CP integration)
letscode-acp [-c config.json]

# Run directly without install
uv run python -m letscode "prompt"
```

Tests run with `uv run pytest` (pytest is in the `dev` dependency group). Tests live under `tests/` and follow a class-grouped pytest style (see `tests/test_prompt.py`).

## Architecture

### Core Loop (`agent.py`)
The agent loop (`run_agent`) streams LLM responses, extracts tool calls, executes them, and feeds results back until the LLM stops requesting tools. Key details:
- Streaming uses line-buffered output to avoid per-token flicker
- Tool call fragments are accumulated by index from streaming chunks
- All tool results go through `_process_tool_result()` — a single processing step that produces the canonical result for ALL outputs (agent context, event log, stdout). This ensures feed replay produces identical behavior
- Results exceeding `RESULT_THRESHOLD` (32KB) are persisted to disk with a preview; the LLM can re-read the full content via the Read tool
- Skill results are split into a tool result ("Launching skill: X") + a user message with the expanded prompt, reflected in both the messages list and a `user_message` event
- MCP tools are merged with built-in tools at startup
- Edit tool enforces read-before-edit: files must be read with Read before Edit is allowed on them (tracked via `_read_files` set)
- `prompt_blocks` parameter accepts structured content blocks (text, resource_link, image) alongside plain text prompts
- Before each LLM call, `cache_markers.apply_cache_markers()` injects `cache_control` breakpoints when the model's `cache` config is `explicit` (no-op for `auto`/`none`); see Prompt Cache Markers below

### Configuration (`config.py`)
Priority: CLI `--model` > config file `default_model` > first model entry. `OPENAI_API_KEY` and `OPENAI_BASE_URL` env vars always override file config. `max_tokens` is capped at 131,072. `list_models()` helper returns all configured models for `--models` CLI flag.

`config.json` schema (two-layer `providers` → `models`; `base_url`/`api_key`/`cache`/`extra_body` belong to the provider, `max_tokens`/`context_window`/`vision` to the model):
```json
{
  "default_model": "model-id",
  "vision_model": "vision-capable-model-id",
  "providers": {
    "<provider>": {
      "base_url": "...",
      "api_key": "...",
      "cache": "auto|explicit|none",
      "extra_body": {"vendor_field": "value"},
      "models": [{"model": "...", "max_tokens": 200000, "context_window": 200000, "vision": false}]
    }
  },
  "mcp_servers": {"name": {"command": "...", "args": [...]} or {"url": "..."}},
  "preset": "safe|default|risk",
  "sandbox": true,
  "rules": {"allowRead": [...], "denyRead": [...], "allowWrite": [...], "denyWrite": [...], "allowCmd": [...], "denyCmd": [...]}
}
```
**Prompt cache mode** (`cache` field, provider- or model-level): `auto` (default) relies on server-side prefix caching — correct for DeepSeek and GLM ≥4.6. `explicit` opts a model into Anthropic-style `cache_control: {type: ephemeral}` markers on content blocks — required for Qwen/DashScope, which returns `cached_tokens=None` without them. `none` disables cache handling. Markers are injected at the single message-assembly site in `agent.py` via `cache_markers.apply_cache_markers()` using the `system_plus_rolling` strategy (3 breakpoints: system + 2nd-to-last + last non-system message) — the only placement achieving zero `cache_creation` across turns in A/B testing (see `docs/cache-multiturn-probe-2026-07-06.md`). The `cache` field cascades provider→model (model overrides provider), matching `base_url`/`api_key`.
**Vendor extension body** (`extra_body` field, provider- or model-level): a dict forwarded verbatim to the OpenAI SDK's `create(extra_body=...)`, landing as top-level fields in the HTTP request body. Use it for provider-specific options that the SDK doesn't model natively. Common case: DashScope's `preserve_thinking` makes Qwen (qwen3.6-plus/3.7-plus only) read the `reasoning_content` replayed from prior turns — by default Qwen silently ignores it (harmless, but no benefit). Note enabling `preserve_thinking` counts prior reasoning toward input tokens and may reduce cache hit rate. Provider- and model-level `extra_body` dicts are deep-merged (model keys override provider keys).
**Reasoning effort** (`effort_options` field, model-level): a list of supported reasoning-effort tier names (e.g. `["high", "max"]`). The first item is the default. When declared, the tier is surfaced as a `thought_level` session config option in ACP (switchable at runtime, persisted per session) and as the CLI `--effort <tier>` flag. The selected tier is injected as `reasoning_effort` into `extra_body` — so it works natively with providers that recognize that field (DeepSeek, GLM-5.2+). Absent means the model exposes no reasoning-effort knob (no ACP option shown, `--effort` has no effect).
**Vision proxy**: `vision: false` on a model + a top-level `vision_model` set → image prompts are first routed through the vision model for text descriptions, spliced back into the prompt (`[Image-N]` markers + appended descriptions), so text-only models can reason about image content. `vision: true` models receive images inline. `call_llm()` (`llm.py`) is the shared single-shot LLM call used here and by future one-off uses (title/summary generation, compaction).
Internally `config.py` flattens `providers` into per-model dicts (merging each provider's `base_url`/`api_key` into its models), so `list_models()`/`load_config()` and their callers see the same flat per-model shape. Note: `rules` keys use camelCase (`allowRead`, `denyCmd`) — not the snake_case names shown in the README.

### Tool System (`tools/`)
Each tool module exposes `SCHEMA` (OpenAI function-calling schema) and `execute(args) -> str`. Registration is in `tools/__init__.py`. Available tools: Bash, Read, Write, Edit, Glob, Grep, Skill, Agent.

- **Agent tool** spawns `letscode` as a subprocess for sub-agent delegation (schema registered dynamically in `agent.py` to avoid circular imports). Defaults: 30 max turns, 300s timeout
- **Grep** prefers system `rg` (ripgrep), falls back to shell `grep -E`; count mode is robust against malformed lines
- **Skill** loads and executes skill files from `.claude/skills/` and `.agents/skills/` directories (`.claude/` takes precedence); supports quoted, multi-line, and colon-containing frontmatter values

### Security Layer (`rules.py`, `sandbox.py`, `tools/runner.py`)
Three-level access control:

1. **Rules engine** (`rules.py`): Glob-based allow/deny rules for paths and commands, loaded from `config.json` `rules` field. Config keys use camelCase: `allowRead`, `denyRead`, `allowWrite`, `denyWrite`, `allowCmd`, `denyCmd`. **Most-specific pattern wins**: a more specific allow overrides a broader deny (the documented "escape hatch"), ties break to deny. `_pattern_specificity(pattern)` ranks patterns by `(anchored, depth, prefix_len)` — literal-path-segment depth before the first wildcard, generalizing nginx's longest-prefix-match. Only `**/`-prefixed pure wildcards rank as unanchored; bare filenames count as relative-anchored (depth 1). Shell expansion detection blocks `$(...)`, backticks, and process substitution. Command splitting handles quoted strings correctly. Secret paths (`.ssh/`, `.aws/`, `.gnupg/`, `.env`) are blocked on all presets (hardcoded baseline, highest priority — overrides any rule). `check_cmd` still uses deny-always-wins (commands have no path hierarchy).

2. **Sandbox** (`sandbox.py`): macOS Seatbelt (`sandbox-exec`) profiles applied to Bash tool subprocesses. Three presets:
   - `safe` — read-only everywhere, no writes
   - `default` — workspace + tmp writable
   - `risk` — full filesystem R/W (secrets still denied)
   - `list_presets()` returns preset metadata for ACP mode selection

3. **Security state** (`tools/runner.py`): Instance attributes on `ToolRunner` (`self._rules`, `self._preset`, `self._sandbox`, `self._tool_allowlist`, `self._skill_allowlist`), set at construction. Tool executors call `check_read` / `check_write` / `check_cmd` before acting.

CLI flags: `--preset safe|default|risk`, `--no-sandbox` to disable entirely.

### ACP Server (`acp/`)
Agent-Client Protocol server using the `agent-client-protocol` SDK, launched via the `letscode-acp` entry point. The server communicates over stdio with a client (e.g. IDE extensions).

- **`server.py`** (`LetscodeAgent`): Implements ACP protocol methods — `initialize`, `new_session`, `prompt`, `cancel`, `load_session`, `list_sessions`, `set_session_mode`, `set_config_option`, `close_session`. The `prompt` method spawns `letscode --event-stream --prompt-format json` as a subprocess and translates its JSONL events into ACP `SessionUpdate` objects via `_translate_event`. Per-turn usage is surfaced both as a `UsageUpdate` (drives the client's context-fill gauge) and, when `show_stat` is set, as a markdown blockquote appended to the agent message with the cache hit rate inline (`> Turn N | Xk tokens (NN%cached) | Ym`); the same footer is reconstructed on session-resume replay via `_make_replay_stat_quote`.
- **`commands.py`**: Slash command registry (`SlashCommandRegistry`) with built-in commands `/new` (clear context), `/compact` (LLM-summarized context compression), `/undo` (roll back last turn). `/compact` preserves skill activation events through compaction. Commands are dispatched before the agent subprocess; results are sent as ACP updates.
- **`session.py`**: Session metadata persistence (`Session` dataclass) stored as JSON in `.letscode/sessions/`. Cursor-based pagination for `list_sessions`.

### MCP Integration (`mcp/client.py`)
Supports stdio, HTTP/SSE, and streamable HTTP MCP servers. Configured in `config.json` under `mcp_servers`. Tools are discovered dynamically and prefixed with `mcp__`. Sub-agents skip MCP (`--no-mcp`) to avoid duplicate connections.

### Event Stream (`events.py`, `stream.py`)
JSONL event emitter for structured output. Always writes to `.letscode/logs/{timestamp}_{uuid}.jsonl`. With `--event-stream`, also outputs to stdout. Event types: `session/prompt`, `agent_message_chunk`, `tool_call`, `tool_call_update`, `user_message`, `error`, `session/result`. `emit_tool_update` records the result as-is (no independent externalization) since results are already processed by `_process_tool_result`. `emit_user_message` emits synthetic user messages (e.g., expanded skill prompts). Data structures (ContentBlock, tool kind, status) are ACP-compatible.

`stream.py` owns the streaming LLM call (`consume_stream_async`) and token-usage normalization. `_normalize_usage()` flattens the per-provider cache field-name variants into a single `(cache_read_tokens, cache_write_tokens, reasoning_tokens)` triple: OpenAI/Qwen/GLM `prompt_tokens_details.cached_tokens`, DeepSeek `prompt_cache_hit_tokens` (note: DeepSeek reports no cache-write field — `prompt_cache_miss_tokens` is the un-cached prefix, not a creation count, so cache_write stays 0), Anthropic `cache_read_input_tokens`/`cache_creation_input_tokens`. The normalized usage flows to `EventHub.record_usage` (session accumulation), the CLI stderr footer (`tokens: 906 (98%cached) in / ...`), the ACP `Usage` object, and the `call_llm` log line.

### Multi-Turn (`feed.py`, `feed_util.py`)
- **`feed.py`**: Loads JSONL event logs and reconstructs the full conversation history (messages list). Used with `--feed <path>` to continue a previous session, optionally with `--append` to write new events into the same log file. Handles `user_message` events for skill content. Legacy logs (with `result_file` externalization and full skill content in `tool_call_update.result`) are backward-compatible.
- **`feed_util.py`**: Shared event log manipulation utilities — `read_events`, `write_events`, `split_turns` (splits at `session/prompt` boundaries), `extract_conversation_text` (generates readable transcript for LLM summarization), `extract_skill_activations` (finds skill prompt events that must survive compaction), `last_agent_text`.

### Prompt Cache Markers (`cache_markers.py`)
Injects `cache_control: {type: ephemeral}` markers into the messages list for providers that need them explicitly (Qwen/DashScope, Anthropic). Called once per turn from `agent.py` after message assembly. Uses the `system_plus_rolling` strategy (3 breakpoints: system + 2nd-to-last + last non-system message), the only placement that achieves zero `cache_creation` across turns in A/B testing. No-op for `cache: auto|none` (DeepSeek, GLM — server-side caching). All helpers are idempotent so feed-replay (which rebuilds messages) never double-marks. See `docs/cache-probe-2026-07-05.md` (per-provider activation) and `docs/cache-multiturn-probe-2026-07-06.md` (strategy A/B test).

### System Prompt (`prompt.py`)
9-section prompt (intro, system, doing_tasks, actions, using_tools, tone_style, output_efficiency, env, skills). `_env_section` dynamically injects CWD, git status, platform, shell, OS version, and model id; `_skills_section` lists discoverable skills (filtered by the card's `skill_allowlist` when active). `build_system_prompt(model_id)` assembles all sections; an AgentCard body replaces this entirely (see AgentCard).

### AgentCard (`agent_card.py`, `builtin_agents/`)
An AgentCard defines a specialized agent persona: its system prompt, available tools, permission boundaries, and MCP/skill access. Cards are Markdown files with YAML frontmatter, loaded via `--as <Name>`:

```bash
letscode --as Review "review tools/runner.py"
```

**Format** (`agents/<Name>.md`):
```markdown
---
name: Reviewer
description: Read-only code review specialist
tools: [Read, Grep, Glob]
skills: [review, lint]
mcp_servers: [playwright]
preset: safe
rules:
  denyWrite: ["/**"]
---
You are a code review specialist. ...
```

**Frontmatter fields** (7): `name`/`description` (metadata), `tools` (whitelist over built-in + `mcp__`-prefixed tools), `skills` (whitelist enforced at Skill execution), `mcp_servers` (whitelist over configured servers), `preset` (sandbox preset), `rules` (camelCase, deep-merged with config.rules). Unset whitelist fields mean "no restriction".

**Single merge point**: `apply_card(config, mcp_servers, card) -> CardOverrides` merges the card onto loaded config once, up front in `cli.py`. `card=None` returns all-default overrides, so the no-card path needs no branching. The 7 card fields do not overlap with existing CLI knobs (`--model`/`--effort`/`--no-sandbox`), except `preset` (`--preset` > card.preset > config.preset) and `mcp_servers` (`--no-mcp` zeros out after the card filter).

**Priority**: CLI flags > AgentCard > `config.json`, per-field independent.

**Built-in cards** ship in `letscode/builtin_agents/` (Explore, Plan, Review, SetupZed), read via `importlib.resources`. A project `agents/<Name>.md` with the same stem overrides a built-in (case-insensitive). `--list-agents` shows all available cards with a `(built-in)` tag. No `--as` → no card → `build_system_prompt(model)` directly (no template layer).

### Template Variables (`prompt_renderer.py`)
AgentCard bodies may reference three predefined variables, rendered only on the `--as` path (the no-card path skips rendering entirely):

- `{{ env }}` — the environment section (CWD/git/platform/shell/OS/model)
- `{{ skills }}` — the available-skills listing (filtered by the card's skill whitelist)
- `{{ default_system_prompt }}` — the full built-in default prompt (escape hatch for cards that want to keep most default behavior and prepend a few lines)

Unknown `{{ names }}` are left untouched. `render_card_template(body, model_id, skill_allowlist)` does the substitution via regex; `_VAR_RE` matches `{{ word }}` with optional inner whitespace.

## Key Design Patterns

- All tool errors are wrapped in `<error>` tags; invalid JSON tool arguments also produce `<error>` results instead of silently defaulting to `{}`
- Tool results are always strings (no structured output)
- Large tool results are persisted to `{log_stem}_results/` instead of truncated
- The config file (`config.json`) holds API keys — do not commit secrets
- The project uses async only for MCP and ACP; the core agent loop is synchronous except where it awaits MCP calls
- Tool SCHEMA and execute are co-located per tool module; dispatch is a simple dict lookup in `tools/__init__.py`
- ACP server delegates to CLI subprocess: server handles protocol/session, subprocess handles agent logic — clean separation of concerns; subprocess is explicitly killed and awaited on exit
- Slash commands operate on the session's JSONL log directly (read/rewrite) before spawning the agent
