# LetsCode

A lightweight, CLI-first AI coding agent harness. Rules-driven, fully customizable, and built for the terminal — loops over any OpenAI-compatible LLM, calling tools until the task is done.

```
$ letscode "add a /health endpoint to app.py"
```

## Why LetsCode?

### True automation, trusted execution

No more step-by-step approvals. Three presets cover most cases out of the box: `safe` (read-only, CI/CD auto-review), `default` (workspace writable, dev work), `risk` (full access, migrations). Set the rules, and the agent runs unattended — overnight refactors, bulk lint fixes, PR auto-review — without you watching, without going off the rails. Need finer control? Custom rules tighten or open up any preset.

```bash
# safe — read-only, CI/CD auto-review
letscode --preset safe "review PR #142"

# default — workspace writable, overnight refactor
letscode --preset default "refactor auth/ to async"

# risk — full access, full migration
letscode --preset risk "migrate db schema to v2"

# advanced — open up writes on top of safe
# config.json:
#   { "preset": "safe", "rules": { "allowWrite": ["./reports/**"] } }
letscode --preset safe "generate test coverage report"
```

### AgentCard: tailor and run

Define ready-to-use agents — tools, MCP servers, skills, permissions — in one Markdown file, then run them with `--as`. No code, no framework — just a card and a flag. Ships four built-in cards (Explore / Plan / Review / SetupZed).

```bash
# Use a built-in card
letscode --as Review "review tools/runner.py"

# List available cards
letscode --list-agents

# Your own card (agents/Refactorer.md) — tools, MCP, skills, permissions in one file:
#   ---
#   name: Refactorer
#   tools: [Read, Edit, Grep, Glob]
#   mcp_servers: [playwright]
#   skills: [refactor]
#   preset: default
#   ---
letscode --as Refactorer "extract a UserService from app.py"
```

### Plug into your favorite editor

Need an interactive session? LetsCode speaks the [Agent Client Protocol](https://agentclientprotocol.com) — the open standard [adopted by Zed](https://zed.dev/blog/acp-registry) and a growing ecosystem of editors. One server, any compatible client, zero lock-in.

```bash
# Zed — built-in agent writes .zed/settings.json for you
letscode --as SetupZed

# JetBrains
letscode --as SetupJetbrains
```

### Composable multi-agent workflows

AgentTeam, dynamic workflows, and beyond. Define roles with AgentCards, compose them at the shell — build any agent workflow you can imagine, Unix-style.

```bash
# Chain agents at the shell: Explore → Plan → Review
letscode --as Explore --event-stream "find all async funcs" \
  | letscode --as Plan --event-stream "draft refactoring plan" \
  | letscode --as Review "review the plan"

# Or let the main agent delegate sub-agents itself
letscode "refactor src/ for async — delegate as needed"
```

## Features

### CLI

- **Any LLM** — Any OpenAI-compatible API: GLM / DeepSeek / Qwen / GPT / local models.
- **Vision proxy** — Text-only models handle images too — set a `vision_model` and images are auto-routed and described in text.
- **OS-level sandbox** — macOS Seatbelt profiles, three presets: `safe` (read-only) / `default` (workspace) / `risk` (full).
- **MCP** — Connect stdio / HTTP / SSE MCP servers; tools are auto-discovered and merged in.
- **Skills** — Write your team's commit / review workflow as a `SKILL.md` and invoke it with a single command.
- **Sub-Agents** — Delegate searches and sub-tasks to a spawned agent so the main context stays clean.
- **Workspace-aware** — `--workspace` switches the working directory; logs, sandbox boundaries, and environment context (cwd / git / shell / platform) bind to it automatically.
- **Project rules** — Follows the [AGENTS.md](https://agents.md) convention — coding standards and workflow rules take effect automatically.
- **JSON event stream** — `--event-stream` emits structured JSONL output; pipe-friendly.
- **Usage stat** — Per-turn token footer (with cache hit rate).

### ACP

- **IDE-integrated** — `letscode-acp` ships a built-in ACP server over stdio; any compatible editor plugs right in.
- **Session management** — Sessions persist across restarts: list / load / resume.
- **Context management** — `/compact` (LLM-summarized context compression) · `/new` (reset) · `/undo` (rollback last turn).

## Install

**One-liner** (installs [uv](https://docs.astral.sh/uv/) if missing, then LetsCode as a global tool):

```bash
curl -fsSL https://raw.githubusercontent.com/letscan/letscode/main/scripts/install.sh | sh
```

Or manually:

```bash
git clone https://github.com/letscan/letscode.git
cd letscode
uv sync                # for development
# — or —
uv tool install git+https://github.com/letscan/letscode.git   # as a global tool
```

Requires Python 3.11+.

## Configuration

Create config from the template and fill in your API key:

```bash
cp config.example.json config.json
```

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

**Works with any OpenAI-compatible API** — GPT, Gemini, GLM, DeepSeek, Qwen, local models, etc.

Environment variables override the config file:

```bash
export OPENAI_API_KEY="YOUR_API_KEY"
export OPENAI_BASE_URL="https://open.bigmodel.cn/api/coding/paas/v4"
```

### Vision

Add `"vision": true` to vision-capable models. For a text-only main model, set a top-level `"vision_model"` — image prompts are routed through it for text descriptions, letting any model handle images:

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

### Prompt caching

Most providers cache the shared prompt prefix automatically — set `"cache": "auto"` (the default, correct for DeepSeek and GLM ≥4.6). Qwen/DashScope needs explicit `cache_control` markers — set `"cache": "explicit"` on the provider:

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

### Security presets

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
