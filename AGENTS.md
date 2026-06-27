# AGENTS.md

This file provides guidance to AI coding agents working with code in this repository.

## Project Overview

letscode is a lightweight Python AI agent harness (v0.2.2) that implements a ReAct-pattern agent loop over OpenAI-compatible APIs. It provides an LLM → Tool Execution → Result Feedback cycle for autonomous software engineering tasks.

- **Language**: Python 3.11+ (managed with `uv`)
- **Core dependencies**: `openai>=1.0`, `mcp>=1.27.0`, `agent-client-protocol>=0.10.0`
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

No test suite exists yet.

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

### Configuration (`config.py`)
Priority: CLI `--model` > config file `default_model` > first model entry. `OPENAI_API_KEY` and `OPENAI_BASE_URL` env vars always override file config. `max_tokens` is capped at 131,072. `list_models()` helper returns all configured models for `--models` CLI flag.

`config.json` schema:
```json
{
  "default_model": "model-id",
  "models": [{"model": "...", "api_key": "...", "base_url": "...", "max_tokens": 200000}],
  "mcp_servers": {"name": {"command": "...", "args": [...]} or {"url": "..."}},
  "preset": "safe|default|risk",
  "sandbox": true,
  "rules": {"allowRead": [...], "denyRead": [...], "allowWrite": [...], "denyWrite": [...], "allowCmd": [...], "denyCmd": [...]}
}
```
Note: `rules` keys use camelCase (`allowRead`, `denyCmd`) — not the snake_case names shown in the README.

### Tool System (`tools/`)
Each tool module exposes `SCHEMA` (OpenAI function-calling schema) and `execute(args) -> str`. Registration is in `tools/__init__.py`. Available tools: Bash, Read, Write, Edit, Glob, Grep, Skill, Agent.

- **Agent tool** spawns `letscode` as a subprocess for sub-agent delegation (schema registered dynamically in `agent.py` to avoid circular imports). Defaults: 30 max turns, 300s timeout
- **Grep** prefers system `rg` (ripgrep), falls back to shell `grep -E`; count mode is robust against malformed lines
- **Skill** loads and executes skill files from `.claude/skills/` and `.agents/skills/` directories (`.claude/` takes precedence); supports quoted, multi-line, and colon-containing frontmatter values

### Security Layer (`rules.py`, `sandbox.py`, `tools/_types.py`)
Three-level access control:

1. **Rules engine** (`rules.py`): Glob-based allow/deny rules for paths and commands, loaded from `config.json` `rules` field. Config keys use camelCase: `allowRead`, `denyRead`, `allowWrite`, `denyWrite`, `allowCmd`, `denyCmd`. Deny rules always override allow rules. Shell expansion detection blocks `$(...)`, backticks, and process substitution. Command splitting handles quoted strings correctly. Secret paths (`.ssh/`, `.aws/`, `.gnupg/`, `.env`) are blocked on all presets.

2. **Sandbox** (`sandbox.py`): macOS Seatbelt (`sandbox-exec`) profiles applied to Bash tool subprocesses. Three presets:
   - `safe` — read-only everywhere, no writes
   - `default` — workspace + tmp writable
   - `risk` — full filesystem R/W (secrets still denied)
   - `list_presets()` returns preset metadata for ACP mode selection

3. **Security state** (`tools/_types.py`): Module-level globals (`_preset`, `_sandbox`, `_rules`) set once at agent startup. Tool executors call `check_read_allowed` / `check_write_allowed` / `check_cmd_allowed` before acting.

CLI flags: `--preset safe|default|risk`, `--no-sandbox` to disable entirely.

### ACP Server (`acp/`)
Agent-Client Protocol server using the `agent-client-protocol` SDK, launched via the `letscode-acp` entry point. The server communicates over stdio with a client (e.g. IDE extensions).

- **`server.py`** (`LetscodeAgent`): Implements ACP protocol methods — `initialize`, `new_session`, `prompt`, `cancel`, `load_session`, `list_sessions`, `set_session_mode`, `set_config_option`, `close_session`. The `prompt` method spawns `letscode --event-stream --prompt-format json` as a subprocess and translates its JSONL events into ACP `SessionUpdate` objects via `_translate_event`.
- **`commands.py`**: Slash command registry (`SlashCommandRegistry`) with built-in commands `/new` (clear context), `/compact` (LLM-summarized context compression), `/undo` (roll back last turn). `/compact` preserves skill activation events through compaction. Commands are dispatched before the agent subprocess; results are sent as ACP updates.
- **`session.py`**: Session metadata persistence (`Session` dataclass) stored as JSON in `.letscode/sessions/`. Cursor-based pagination for `list_sessions`.

### MCP Integration (`mcp/client.py`)
Supports stdio, HTTP/SSE, and streamable HTTP MCP servers. Configured in `config.json` under `mcp_servers`. Tools are discovered dynamically and prefixed with `mcp__`. Sub-agents skip MCP (`--no-mcp`) to avoid duplicate connections.

### Event Stream (`events.py`)
JSONL event emitter for structured output. Always writes to `.letscode/logs/{timestamp}_{uuid}.jsonl`. With `--event-stream`, also outputs to stdout. Event types: `session/prompt`, `agent_message_chunk`, `tool_call`, `tool_call_update`, `user_message`, `error`, `session/result`. `emit_tool_update` records the result as-is (no independent externalization) since results are already processed by `_process_tool_result`. `emit_user_message` emits synthetic user messages (e.g., expanded skill prompts). Data structures (ContentBlock, tool kind, status) are ACP-compatible.

### Multi-Turn (`feed.py`, `feed_util.py`)
- **`feed.py`**: Loads JSONL event logs and reconstructs the full conversation history (messages list). Used with `--feed <path>` to continue a previous session, optionally with `--append` to write new events into the same log file. Handles `user_message` events for skill content. Legacy logs (with `result_file` externalization and full skill content in `tool_call_update.result`) are backward-compatible.
- **`feed_util.py`**: Shared event log manipulation utilities — `read_events`, `write_events`, `split_turns` (splits at `session/prompt` boundaries), `extract_conversation_text` (generates readable transcript for LLM summarization), `extract_skill_activations` (finds skill prompt events that must survive compaction), `last_agent_text`.

### System Prompt (`prompt.py`)
8-section prompt. The `_env_section` dynamically injects CWD, git status, platform, shell, and OS version at runtime.

## Key Design Patterns

- All tool errors are wrapped in `<error>` tags; invalid JSON tool arguments also produce `<error>` results instead of silently defaulting to `{}`
- Tool results are always strings (no structured output)
- Large tool results are persisted to `{log_stem}_results/` instead of truncated
- The config file (`config.json`) holds API keys — do not commit secrets
- The project uses async only for MCP and ACP; the core agent loop is synchronous except where it awaits MCP calls
- Tool SCHEMA and execute are co-located per tool module; dispatch is a simple dict lookup in `tools/__init__.py`
- ACP server delegates to CLI subprocess: server handles protocol/session, subprocess handles agent logic — clean separation of concerns; subprocess is explicitly killed and awaited on exit
- Slash commands operate on the session's JSONL log directly (read/rewrite) before spawning the agent
