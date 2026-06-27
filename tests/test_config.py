"""Tests for model resolution in load_config.

The key invariant: an explicit-but-invalid model id must fail loudly, not
silently fall back to models[0]. Silent fallback masked typos that routed
vision prompts to text-only models (the LLM then answered "I see no image").
"""

import json

import pytest

from letscode.config import load_config, list_models


def _write_config(tmp_path, models, default_model=None):
    cfg = {"models": models}
    if default_model is not None:
        cfg["default_model"] = default_model
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


class TestModelResolution:
    """An explicit model id resolves to that entry; a typo raises, not falls back."""

    def test_explicit_valid_model(self, tmp_path):
        path = _write_config(tmp_path, [
            {"model": "a", "api_key": "k", "base_url": "u"},
            {"model": "b", "api_key": "k", "base_url": "u"},
        ])
        cfg, _ = load_config(path, "b")
        assert cfg.model == "b"

    def test_explicit_invalid_model_raises(self, tmp_path):
        # The regression: a typo (e.g. "qwen-3.6-27b" vs "qwen3.6-27b") must NOT
        # silently fall back to models[0].
        path = _write_config(tmp_path, [
            {"model": "a", "api_key": "k", "base_url": "u"},
            {"model": "b", "api_key": "k", "base_url": "u"},
        ])
        with pytest.raises(SystemExit) as ei:
            load_config(path, "typo-model")
        msg = str(ei.value)
        assert "typo-model" in msg
        # Error message lists available models to help the user correct the typo.
        assert "a" in msg and "b" in msg

    def test_no_model_uses_default(self, tmp_path):
        path = _write_config(tmp_path, [
            {"model": "a", "api_key": "k", "base_url": "u"},
            {"model": "b", "api_key": "k", "base_url": "u"},
        ], default_model="b")
        cfg, _ = load_config(path, None)
        assert cfg.model == "b"

    def test_no_model_no_default_falls_back_to_first(self, tmp_path):
        # With no model id and no default, using the first entry is a reasonable
        # default — this fallback is intentional and unchanged.
        path = _write_config(tmp_path, [
            {"model": "first", "api_key": "k", "base_url": "u"},
            {"model": "second", "api_key": "k", "base_url": "u"},
        ])
        cfg, _ = load_config(path, None)
        assert cfg.model == "first"

    def test_default_model_typo_also_raises(self, tmp_path):
        # A typo in default_model (from the config file itself) is the same
        # class of silent failure — must also raise.
        path = _write_config(tmp_path, [
            {"model": "a", "api_key": "k", "base_url": "u"},
        ], default_model="typo")
        with pytest.raises(SystemExit) as ei:
            load_config(path, None)
        assert "typo" in str(ei.value)

    def test_no_models_no_target_raises(self, tmp_path):
        path = _write_config(tmp_path, [])
        with pytest.raises(SystemExit):
            load_config(path, None)


class TestListModels:
    def test_returns_models_and_default(self, tmp_path):
        path = _write_config(tmp_path, [
            {"model": "a", "api_key": "k", "base_url": "u"},
        ], default_model="a")
        models, default = list_models(path)
        assert default == "a"
        assert [m["model"] for m in models] == ["a"]
