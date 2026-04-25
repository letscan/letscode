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


# MCP server config: either {command, args?, env?} for stdio or {url, headers?} for http/sse
type McpServerConfig = dict[str, any]


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
    models: list[dict] = []
    default_model: str | None = None
    mcp_servers: dict[str, McpServerConfig] = {}

    if config_path:
        with open(config_path) as f:
            data = json.load(f)
        models = data.get("models", [])
        default_model = data.get("default_model")
        mcp_servers = data.get("mcp_servers", {})

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
        )
    elif target:
        cfg = ModelConfig(model=target)
    else:
        raise SystemExit(
            "No model specified. Use --model or set default_model in config file."
        )

    if api_key := os.environ.get("OPENAI_API_KEY"):
        cfg.api_key = api_key
    if base_url := os.environ.get("OPENAI_BASE_URL"):
        cfg.base_url = base_url

    return cfg, mcp_servers
