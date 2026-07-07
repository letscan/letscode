"""Tests for the two-layer providers → models config.

Config format:
    {"providers": {"<name>": {"base_url", "api_key", "models": [{"model", ...}]}}}

config.py flattens providers into per-model dicts internally, so these tests
also cover that flattening (provider base_url/api_key merged into each model,
model-level fields preserved) on top of the resolution invariants.
"""

import json

import pytest

from letscode.config import load_config, list_models, load_vision_model_id


def _write_config(tmp_path, providers, default_model=None):
    """Write a providers-structured config file.

    ``providers`` is a dict like {"name": {"base_url":..., "api_key":...,
    "models": [{"model":..., "max_tokens":...}]}}.
    """
    cfg = {"providers": providers}
    if default_model is not None:
        cfg["default_model"] = default_model
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def _provider(base_url, api_key, models):
    """Build one provider entry with the given model dicts."""
    return {"base_url": base_url, "api_key": api_key, "models": models}


class TestModelResolution:
    """An explicit model id resolves to that entry; a typo raises, not falls back."""

    def test_explicit_valid_model(self, tmp_path):
        path = _write_config(tmp_path, {
            "p1": _provider("u", "k1", [{"model": "a"}, {"model": "b"}]),
        })
        cfg, _ = load_config(path, "b")
        assert cfg.model == "b"

    def test_explicit_invalid_model_raises(self, tmp_path):
        # The regression: a typo must NOT silently fall back to the first model.
        path = _write_config(tmp_path, {
            "p1": _provider("u", "k1", [{"model": "a"}, {"model": "b"}]),
        })
        with pytest.raises(SystemExit) as ei:
            load_config(path, "typo-model")
        msg = str(ei.value)
        assert "typo-model" in msg
        # Error message lists available models to help the user correct the typo.
        assert "a" in msg and "b" in msg

    def test_no_model_uses_default(self, tmp_path):
        path = _write_config(tmp_path, {
            "p1": _provider("u", "k1", [{"model": "a"}, {"model": "b"}]),
        }, default_model="b")
        cfg, _ = load_config(path, None)
        assert cfg.model == "b"

    def test_no_model_no_default_falls_back_to_first(self, tmp_path):
        # With no model id and no default, using the first model is a reasonable
        # default — this fallback is intentional and unchanged.
        path = _write_config(tmp_path, {
            "p1": _provider("u", "k1", [{"model": "first"}]),
            "p2": _provider("u2", "k2", [{"model": "second"}]),
        })
        cfg, _ = load_config(path, None)
        assert cfg.model == "first"

    def test_default_model_typo_also_raises(self, tmp_path):
        path = _write_config(tmp_path, {
            "p1": _provider("u", "k1", [{"model": "a"}]),
        }, default_model="typo")
        with pytest.raises(SystemExit) as ei:
            load_config(path, None)
        assert "typo" in str(ei.value)

    def test_no_models_no_target_raises(self, tmp_path):
        path = _write_config(tmp_path, {})
        with pytest.raises(SystemExit):
            load_config(path, None)


class TestProviderFlattening:
    """Provider base_url/api_key merge into each model; model fields preserved."""

    def test_provider_fields_merged_into_model(self, tmp_path):
        path = _write_config(tmp_path, {
            "zhipu": _provider("https://api.zhipu/v4", "sk-z", [
                {"model": "glm-a", "max_tokens": 100, "context_window": 1000},
                {"model": "glm-b", "max_tokens": 200},
            ]),
        })
        cfg_a, _ = load_config(path, "glm-a")
        assert cfg_a.base_url == "https://api.zhipu/v4"
        assert cfg_a.api_key == "sk-z"
        assert cfg_a.max_tokens == 100 and cfg_a.context_window == 1000

        cfg_b, _ = load_config(path, "glm-b")
        assert cfg_b.base_url == "https://api.zhipu/v4"  # same provider
        assert cfg_b.api_key == "sk-z"                    # same provider
        assert cfg_b.context_window is None               # unset on model

    def test_provider_without_base_url_uses_default(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider(None, "k", [{"model": "a"}]),  # type: ignore[arg-type]
        })
        cfg, _ = load_config(path, "a")
        assert cfg.base_url == "https://api.openai.com/v1"

    def test_list_models_returns_flat_dicts(self, tmp_path):
        path = _write_config(tmp_path, {
            "p1": _provider("u1", "k1", [{"model": "a", "context_window": 5000}]),
            "p2": _provider("u2", "k2", [{"model": "b"}]),
        })
        models, default = list_models(path)
        assert [m["model"] for m in models] == ["a", "b"]
        # Flat dicts carry merged provider fields + model-level fields.
        a = next(m for m in models if m["model"] == "a")
        assert a["base_url"] == "u1" and a["api_key"] == "k1" and a["context_window"] == 5000


class TestVisionFields:
    """vision flag on models + vision_model id at the top level."""

    def test_vision_defaults_false(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a"}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.vision is False

    def test_vision_true_loaded(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a", "vision": True}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.vision is True

    def test_load_vision_model_id(self, tmp_path):
        import json as _json
        p = tmp_path / "config.json"
        p.write_text(_json.dumps({
            "vision_model": "glm-4.6v-flash",
            "providers": {"p": _provider("u", "k", [{"model": "m"}])},
        }))
        assert load_vision_model_id(str(p)) == "glm-4.6v-flash"

    def test_load_vision_model_id_none_when_unset(self, tmp_path):
        path = _write_config(tmp_path, {"p": _provider("u", "k", [{"model": "m"}])})
        assert load_vision_model_id(path) is None


class TestCacheFields:
    """cache flag on models — controls cache_control marker injection.

    Default "auto" (DeepSeek/GLM cache server-side, no markers needed);
    "explicit" opts a model into per-block markers (Qwen/DashScope, Anthropic).
    """

    def test_cache_defaults_auto(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a"}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.cache == "auto"

    def test_cache_explicit_loaded(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a", "cache": "explicit"}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.cache == "explicit"

    def test_cache_none_loaded(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a", "cache": "none"}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.cache == "none"

    def test_cache_provider_level_cascades(self, tmp_path):
        # Provider-level cache applies to models that don't override it.
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k", "cache": "explicit",
                  "models": [{"model": "a"}, {"model": "b", "cache": "auto"}]},
        })
        cfg_a, _ = load_config(path, "a")
        cfg_b, _ = load_config(path, "b")
        assert cfg_a.cache == "explicit"  # inherited from provider
        assert cfg_b.cache == "auto"      # model-level overrides provider

    def test_cache_model_overrides_provider(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k", "cache": "auto",
                  "models": [{"model": "a", "cache": "explicit"}]},
        })
        cfg, _ = load_config(path, "a")
        assert cfg.cache == "explicit"


class TestExtraBody:
    """extra_body — a dict forwarded verbatim to the API request body.

    Lets a user inject vendor extensions (e.g. DashScope's preserve_thinking)
    without code changes. Provider- and model-level dicts are deep-merged,
    model keys overriding provider keys.
    """

    def test_defaults_none(self, tmp_path):
        path = _write_config(tmp_path, {"p": _provider("u", "k", [{"model": "a"}])})
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body is None

    def test_provider_level_loaded(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k",
                  "extra_body": {"preserve_thinking": True},
                  "models": [{"model": "a"}]},
        })
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body == {"preserve_thinking": True}

    def test_model_level_loaded(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a",
                                       "extra_body": {"enable_thinking": False}}]),
        })
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body == {"enable_thinking": False}

    def test_provider_model_deep_merge(self, tmp_path):
        # Provider sets one key, model sets another → both present.
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k",
                  "extra_body": {"preserve_thinking": True},
                  "models": [{"model": "a",
                              "extra_body": {"enable_thinking": False}}]},
        })
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body == {"preserve_thinking": True, "enable_thinking": False}

    def test_model_overrides_provider_same_key(self, tmp_path):
        # Same key on both → model wins.
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k",
                  "extra_body": {"preserve_thinking": True},
                  "models": [{"model": "a",
                              "extra_body": {"preserve_thinking": False}}]},
        })
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body == {"preserve_thinking": False}

    def test_no_extra_body_no_crash_on_merge(self, tmp_path):
        # Provider has extra_body, model has none → provider wins, no crash.
        path = _write_config(tmp_path, {
            "p": {"base_url": "u", "api_key": "k",
                  "extra_body": {"preserve_thinking": True},
                  "models": [{"model": "a"}]},
        })
        cfg, _ = load_config(path, "a")
        assert cfg.extra_body == {"preserve_thinking": True}


class TestListModels:
    def test_returns_models_and_default(self, tmp_path):
        path = _write_config(tmp_path, {
            "p": _provider("u", "k", [{"model": "a"}]),
        }, default_model="a")
        models, default = list_models(path)
        assert default == "a"
        assert [m["model"] for m in models] == ["a"]

