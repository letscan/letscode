# letscode

A lightweight, CLI-first AI coding agent. A single Python script that loops over any OpenAI-compatible LLM, calling tools until the task is done.

```
$ letscode "add a /health endpoint to app.py"
```

**Why letscode?**

- **Lightweight** — ~2K lines of Python, four dependencies (`openai`, `mcp`, `agent-client-protocol`, `pyyaml`). No vector databases, no framework lock-in.
- **CLI-first** — Runs in your terminal. Pipe prompts in, read structured JSONL out. Fits into any workflow.
- **ACP-ready** — Ships `letscode-acp`, an [Agent Client Protocol](https://github.com/AI-Utils/agent-client-protocol) server for IDE and client integration (VS Code extensions, etc.).

## Features

- **ReAct agent loop** — LLM calls tools, sees results, decides when to stop
- **8 built-in tools** — Bash, Read, Write, Edit, Glob, Grep, Skill, Agent (sub-agent delegation)
- **AgentCards** — Define specialized agents (reviewer, planner, explorer) as Markdown + YAML; ships built-in Explore/Plan/Review/SetupZed
- **MCP integration** — Connect stdio and HTTP/SSE MCP servers for extra tools
- **3-layer security** — Rule engine (path/command allowlist) + macOS Seatbelt sandbox + tool-level permission checks
- **Event stream output** — JSONL structured logs, ACP-compatible
- **Multi-turn sessions** — Resume from previous session logs with `--feed`
- **Slash commands** — `/new` reset context, `/compact` compress context, `/undo` rollback last turn
- **Prompt caching** — Per-model `cache` config (`auto`/`explicit`/`none`) with `cache_control` marker injection for providers that need it (Qwen/DashScope); per-turn cache hit rate shown in the stat footer

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/letscan/letscode.git
cd letscode
uv sync
```

Or install as a tool:

```bash
uv tool install .
```

## Quick Start

### 1. Create config

```bash
cp config.example.json config.json
```

Edit `config.json` with your API key:

```json
{
  "default_model": "glm-5-turbo",
  "providers": {
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "api_key": "YOUR_API_KEY",
      "models": [
        { "model": "glm-5-turbo", "max_tokens": 200000 }
      ]
    }
  }
}
```

`base_url` and `api_key` belong to the provider; multiple models under the same provider share them.

Add `"vision": true` to vision-capable models. For a text-only main model, set a top-level `"vision_model"` — image prompts are then routed through it for descriptions, letting any model handle images:

```json
{
  "default_model": "glm-5-turbo",
  "vision_model": "glm-4.6v-flash",
  "providers": {
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "api_key": "YOUR_API_KEY",
      "models": [
        { "model": "glm-5-turbo", "max_tokens": 200000, "vision": false },
        { "model": "glm-4.6v-flash", "max_tokens": 32768, "vision": true }
      ]
    }
  }
}
```

Environment variables override the config file:

```bash
export OPENAI_API_KEY="YOUR_API_KEY"
export OPENAI_BASE_URL="https://open.bigmodel.cn/api/coding/paas/v4"
```

Works with any OpenAI-compatible API (GPT, Gemini, GLM, DeepSeek, Qwen, local models, etc.).

**Prompt caching:** most providers cache the shared prompt prefix automatically — set `"cache": "auto"` (the default, correct for DeepSeek and GLM ≥4.6). Qwen/DashScope needs explicit `cache_control` markers, so set `"cache": "explicit"` on the provider:

```json
{
  "providers": {
    "dashscope": {
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "api_key": "YOUR_DASHSCOPE_API_KEY",
      "cache": "explicit",
      "models": [{"model": "qwen3.5-plus-2026-04-20", "max_tokens": 65536}]
    }
  }
}
```

The cache hit rate then shows inline in the per-turn stat footer (`2.7k tokens (99%cached)`).

### 2. Run

```bash
# Via uv
uv run letscode "create a Python web server"

# After install
letscode "your task"
```

### 3. ACP Server

```bash
letscode-acp [-c config.json]
```

Communicates over stdio with any ACP client.

## Usage

```
letscode [options] "prompt"
```

| Option | Description |
|--------|-------------|
| `-c, --config` | Config file path (default: `config.json`) |
| `-m, --model` | Model ID (overrides config default) |
| `-w, --workspace` | Working directory (default: cwd) |
| `--max-turns` | Max conversation turns |
| `--preset` | Security preset: `safe` / `default` / `risk` |
| `--no-sandbox` | Disable macOS sandbox |
| `--no-mcp` | Skip MCP server connections |
| `-v, --verbose` | Show tool call details |
| `--models` | List available models from config |
| `--list-agents` | List available agent cards (built-in + project) |
| `--as <name>` | Run as a specific agent card (replaces system prompt, restricts tools/rules) |
| `--event-stream` | Output as JSONL event stream |
| `--prompt-format` | Prompt format: `text` (default) or `json` (structured content blocks) |
| `--feed` | Resume from a previous session log |
| `--append` | Append new events to the same log file |

### Examples

```bash
# List models
letscode --models

# Use a specific model with verbose output
letscode -m glm-4.6 -v "refactor src/"

# Structured input (ACP-compatible)
letscode --prompt-format json '[{"type":"text","text":"hello"}]'

# Resume a session
letscode --feed .letscode/logs/session.jsonl --append "continue the task"
```

## Security

| Preset | Read | Write | Commands |
|--------|------|-------|----------|
| `safe` | Global | Blocked | Blocked |
| `default` | Global | Workspace + /tmp | Whitelisted |
| `risk` | Global | Global | All allowed |

Secret paths (`.ssh/`, `.aws/`, `.gnupg/`, `.env`) are blocked on all presets.

Custom rules in `config.json` (keys are camelCase):

```json
{
  "rules": {
    "allowRead": ["src/**", "tests/**"],
    "denyWrite": ["secrets/**"],
    "allowCmd": ["ls", "cat", "git"],
    "denyCmd": ["rm -rf"]
  }
}
```

Rules use **most-specific-wins**: a more specific allow (e.g. `plan.md`) overrides a broader deny (e.g. `/**`), ties break to deny. This lets AgentCards pair `preset: safe` with a narrow `allowWrite` to carve out write access for specific files.

## AgentCards

An AgentCard defines a specialized agent: its system prompt, tool whitelist, and permission boundary. Create one as `agents/<Name>.md`:

```markdown
---
name: Reviewer
description: Read-only code review specialist
tools: [Read, Grep, Glob]
preset: safe
rules:
  denyWrite: ["/**"]
---
You are a code review specialist. Cite file:line for every comment.

{{ env }}
```

Run with `--as`:

```bash
letscode --as Reviewer "review tools/runner.py"
```

**Built-in cards** ship with letscode: `Explore` (read-only codebase search), `Plan` (investigate + write a plan file), `Review` (read-only code review), `SetupZed` (configure Zed editor integration). List them with `letscode --list-agents`. A project `agents/<Name>.md` overrides a built-in of the same name.

Card bodies support three template variables: `{{ env }}`, `{{ skills }}`, and `{{ default_system_prompt }}` (the full built-in prompt — useful when you want to keep most defaults and prepend a few lines).

**Priority**: CLI flags > AgentCard > `config.json`, per-field. For example `--preset risk` overrides the card's `preset: safe`.

## Architecture

```
CLI input → Agent loop → Tool execution → Result feedback
               │
               ├── Config (config.py)
               ├── MCP tools (mcp/)
               ├── Event stream (events.py)
               └── Security (rules.py + sandbox.py)
```

## License

MIT
