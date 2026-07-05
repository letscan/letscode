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
    vision: bool = False
    cache: str = "auto"


# MCP server config: either {command, args?, env?} for stdio or {url, headers?} for http/sse
type McpServerConfig = dict[str, any]


def _load_config_file(config_path: str | None) -> tuple:
    """Parse config file and return raw fields.

    The file uses a two-layer ``providers`` → ``models`` structure::

        "providers": {
          "<provider>": {
            "base_url": "...", "api_key": "...",
            "models": [{"model": "...", "max_tokens": ..., "context_window": ...}]
          }
        }

    Here we flatten that into a list of per-model dicts (merging the provider's
    ``base_url``/``api_key`` into each model entry), so the rest of the code —
    model lookup, ModelConfig construction, ``list_models`` — keeps working with
    the same flat per-model dict shape as before.
    """
    models: list[dict] = []
    default_model: str | None = None
    vision_model: str | None = None
    mcp_servers: dict[str, McpServerConfig] = {}
    sandbox_preset: str = "default"
    sandbox: bool = True
    rules: dict | None = None

    if config_path:
        with open(config_path) as f:
            data = json.load(f)
        default_model = data.get("default_model")
        vision_model = data.get("vision_model")
        mcp_servers = data.get("mcp_servers", {})
        sandbox_preset = data.get("preset", "default")
        sandbox = data.get("sandbox", True)
        rules = data.get("rules")
        models = _flatten_providers(data.get("providers", {}))

    return models, default_model, vision_model, mcp_servers, sandbox_preset, sandbox, rules


def _flatten_providers(providers: dict) -> list[dict]:
    """Expand the ``providers`` dict into a flat list of per-model dicts.

    Each provider contributes its ``base_url``/``api_key`` to every model listed
    under it; per-model fields (``max_tokens``, ``max_retries``,
    ``context_window``) stay on the model. Model entries with no ``model`` key
    are skipped.
    """
    flat: list[dict] = []
    if not isinstance(providers, dict):
        return flat
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        base_url = provider.get("base_url") or "https://api.openai.com/v1"
        api_key = provider.get("api_key")
        for m in provider.get("models", []) or []:
            if not isinstance(m, dict) or not m.get("model"):
                continue
            entry = dict(m)  # model-level fields first
            entry.setdefault("api_key", api_key)
            entry.setdefault("base_url", base_url)
            # Provider-level cache cascades only when actually set, so an unset
            # provider value doesn't shadow the model-level default ("auto").
            p_cache = provider.get("cache")
            if p_cache is not None:
                entry.setdefault("cache", p_cache)
            flat.append(entry)
    return flat


def list_models(config_path: str | None) -> tuple[list[dict], str | None]:
    """Return (models_list, default_model) from config."""
    models, default_model, *_ = _load_config_file(config_path)
    return models, default_model


def load_vision_model_id(config_path: str | None) -> str | None:
    """Return the configured ``vision_model`` id (top-level), or None."""
    _, _, vision_model, *_ = _load_config_file(config_path)
    return vision_model


def load_config(
    config_path: str | None, model_id: str | None = None,
) -> tuple[ModelConfig, dict[str, McpServerConfig]]:
    """Load model config and MCP server configs.

    Returns (ModelConfig, {server_name: server_config}).

    Config file format:
    {
        "default_model": "...",
        "providers": {
            "<provider>": {
                "base_url": "...", "api_key": "...",
                "models": [{"model": "...", "max_tokens": 200000, "context_window": ...}]
            }
        },
        "mcp_servers": {
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp"]},
            "exa": {"url": "https://mcp.exa.ai/mcp", "headers": {...}}
        }
    }
    """
    models, default_model, vision_model, mcp_servers, sandbox_preset, sandbox, rules = _load_config_file(config_path)

    target = model_id or default_model

    entry: dict | None = None
    if target:
        for m in models:
            if m.get("model") == target:
                entry = m
                break

    if entry is None:
        if target:
            # An explicit model id was given but isn't in the config. Failing
            # loudly here beats silently falling back to models[0] — which has
            # masked typos (e.g. sending a vision prompt to a text-only model).
            available = ", ".join(m.get("model", "?") for m in models) or "(none)"
            raise SystemExit(
                f"Model {target!r} not found in config. Available: {available}"
            )
        elif models:
            # No model specified at all — use the first entry as the default.
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
            vision=bool(entry.get("vision", False)),
            cache=entry.get("cache", "auto"),
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
