# AGENTS.md

This file provides guidance to letscode (letscan.ai/code) when working with code in this repository.

## Project Overview

letscode is a lightweight Python AI agent harness (v0.1.0) that implements a ReAct-pattern agent loop over OpenAI-compatible APIs. It provides an LLM → Tool Execution → Result Feedback cycle for autonomous software engineering tasks.

- **Language**: Python 3.11+ (managed with `uv`)
- **Core dependencies**: `openai>=1.0`, `mcp>=1.27.0`
- **Default model**: GLM-5-Turbo via 智谱AI API

## Common Commands

```bash
# Run the agent
letscode "your prompt here"
python -m letscode "your prompt here"

# With options
letscode -c config.json -m glm-5-turbo -w /path/to/workspace -v "prompt"

# Event stream mode (JSONL to stdout)
letscode --event-stream "prompt"

# Multi-turn: continue from a previous session log
letscode --feed .letscode/logs/20260501_abcd.jsonl --append "follow-up prompt"

# Install dependencies (uv required)
uv sync

# Run directly without install
uv run python -m letscode "prompt"
```

No test suite exists yet.

## Architecture

### Core Loop (`agent.py`)
The agent loop (`run_agent`) streams LLM responses, extracts tool calls, executes them, and feeds results back until the LLM stops requesting tools. Key details:
- Streaming uses line-buffered output to avoid per-token flicker
- Tool call fragments are accumulated by index from streaming chunks
- Tool results exceeding 50,000 chars are truncated
- MCP tools are merged with built-in tools at startup
- Edit tool enforces read-before-edit: files must be read with Read before Edit is allowed on them (tracked via `_read_files` set)
- Skill tool inlines result as a user message rather than a plain tool result, so the LLM sees the skill's expanded prompt directly

### Configuration (`config.py`)
Priority: CLI `--model` > config file `default_model` > first model entry. `OPENAI_API_KEY` and `OPENAI_BASE_URL` env vars always override file config. `max_tokens` is capped at 131,072.

### Tool System (`tools/`)
Each tool module exposes `SCHEMA` (OpenAI function-calling schema) and `execute(args) -> str`. Registration is in `tools/__init__.py`. Available tools: Bash, Read, Write, Edit, Glob, Grep, Skill, Agent.

- **Agent tool** spawns `letscode` as a subprocess for sub-agent delegation (schema registered dynamically in `agent.py` to avoid circular imports)
- **Grep** prefers system `rg` (ripgrep), falls back to shell `grep -E`
- **Skill** loads and executes skill files from `.claude/skills/` directories

### Security Layer (`rules.py`, `sandbox.py`, `tools/_types.py`)
Three-level access control:

1. **Rules engine** (`rules.py`): Glob-based allow/deny rules for paths and commands, loaded from `config.json` `rules` field. Deny rules always override allow rules. Secret paths (`.ssh/`, `.aws/`, `.gnupg/`, `.env`) are blocked on all presets.

2. **Sandbox** (`sandbox.py`): macOS Seatbelt (`sandbox-exec`) profiles applied to Bash tool subprocesses. Three presets:
   - `safe` — read-only everywhere, no writes
   - `default` — workspace + tmp writable
   - `risk` — full filesystem R/W (secrets still denied)

3. **Security state** (`tools/_types.py`): Module-level globals (`_preset`, `_sandbox`, `_rules`) set once at agent startup. Tool executors call `check_read_allowed` / `check_write_allowed` / `check_cmd_allowed` before acting.

CLI flags: `--preset safe|default|risk`, `--no-sandbox` to disable entirely.

### MCP Integration (`mcp/client.py`)
Supports stdio and HTTP/SSE MCP servers. Configured in `config.json` under `mcp_servers`. Tools are discovered dynamically and prefixed with `mcp__`. Sub-agents skip MCP (`--no-mcp`) to avoid duplicate connections.

### Event Stream (`events.py`)
JSONL event emitter for structured output. Always writes to `.letscode/logs/{timestamp}_{uuid}.jsonl`. With `--event-stream`, also outputs to stdout. Event types: `session.start`, `user_prompt`, `agent_message`, `tool_call`, `tool_call_update`, `error`, `session.end`. Data structures (ContentBlock, tool kind, status) are ACP-compatible.

### Multi-Turn (`feed.py`)
Loads JSONL event logs and reconstructs the full conversation history (messages list). Used with `--feed <path>` to continue a previous session, optionally with `--append` to write new events into the same log file.

### System Prompt (`prompt.py`)
8-section prompt built from letscode's `src/constants/prompts.ts`. The `_env_section` dynamically injects CWD, git status, platform, shell, and OS version at runtime.

## Key Design Patterns

- All tool errors are wrapped in `<error>` tags
- Tool results are always strings (no structured output)
- The config file (`config.json`) holds API keys — do not commit secrets
- The project uses async only for MCP; the core agent loop is synchronous except where it awaits MCP calls
- Tool SCHEMA and execute are co-located per tool module; dispatch is a simple dict lookup in `tools/__init__.py`
