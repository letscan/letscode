"""Configuration loading for letscode."""

import json
import os
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    model: str
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    max_tokens: int = 16384
    max_retries: int = 3
    context_window: int | None = None
    preset: str = "default"
    sandbox: bool = True
    verbose: bool = False
    rules: dict | None = None


# MCP server config: either {command, args?, env?} for stdio or {url, headers?} for http/sse
type McpServerConfig = dict[str, any]


def _load_config_file(config_path: str | None) -> tuple:
    """Parse config file and return raw fields."""
    models: list[dict] = []
    default_model: str | None = None
    mcp_servers: dict[str, McpServerConfig] = {}
    sandbox_preset: str = "default"
    sandbox: bool = True
    rules: dict | None = None

    if config_path:
        with open(config_path) as f:
            data = json.load(f)
        models = data.get("models", [])
        default_model = data.get("default_model")
        mcp_servers = data.get("mcp_servers", {})
        sandbox_preset = data.get("preset", "default")
        sandbox = data.get("sandbox", True)
        rules = data.get("rules")

    return models, default_model, mcp_servers, sandbox_preset, sandbox, rules


def list_models(config_path: str | None) -> tuple[list[dict], str | None]:
    """Return (models_list, default_model) from config."""
    models, default_model, *_ = _load_config_file(config_path)
    return models, default_model


def load_config(
    config_path: str | None, model_id: str | None = None,
) -> tuple[ModelConfig, dict[str, McpServerConfig]]:
    """Load model config and MCP server configs.

    Returns (ModelConfig, {server_name: server_config}).

    Config file format:
    {
        "default_model": "...",
        "models": [...],
        "mcp_servers": {
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp"]},
            "exa": {"url": "https://mcp.exa.ai/mcp", "headers": {...}}
        }
    }
    """
    models, default_model, mcp_servers, sandbox_preset, sandbox, rules = _load_config_file(config_path)

    target = model_id or default_model

    entry: dict | None = None
    if target:
        for m in models:
            if m.get("model") == target:
                entry = m
                break

    if entry is None and models:
        entry = models[0]

    if entry:
        max_tokens = min(entry.get("max_tokens", 16384), 131072)
        cfg = ModelConfig(
            model=entry["model"],
            api_key=entry.get("api_key"),
            base_url=entry.get("base_url", "https://api.openai.com/v1"),
            max_tokens=max_tokens,
            max_retries=entry.get("max_retries", 3),
            context_window=entry.get("context_window"),
            preset=sandbox_preset,
            sandbox=sandbox,
            rules=rules,
        )
    elif target:
        cfg = ModelConfig(model=target, preset=sandbox_preset, sandbox=sandbox, rules=rules)
    else:
        raise SystemExit(
            "No model specified. Use --model or set default_model in config file."
        )

    if api_key := os.environ.get("OPENAI_API_KEY"):
        cfg.api_key = api_key
    if base_url := os.environ.get("OPENAI_BASE_URL"):
        cfg.base_url = base_url

    return cfg, mcp_servers
