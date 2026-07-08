"""Tests for the rules engine: specificity ranking and most-specific-wins.

The engine ranks competing allow/deny matches by pattern specificity (nginx
longest-prefix-match generalized): a more specific allow overrides a broader
deny, ties break to deny. Secrets baseline always wins.
"""

import os

import pytest

from letscode.rules import (
    Rules,
    _pattern_specificity,
    check_read,
    check_write,
    merge_rules,
)


# ---------------------------------------------------------------------------
# Specificity computation
# ---------------------------------------------------------------------------

class TestPatternSpecificity:
    """_pattern_specificity returns (anchored, depth, prefix_len); larger = more specific."""

    @pytest.mark.parametrize("pattern, expected", [
        ("/**", (1, 0, 1)),
        ("/a/*", (1, 1, 3)),
        ("/a/b/*", (1, 2, 5)),
        ("plan.md", (1, 1, 7)),           # bare filename: depth 1 (one literal segment)
        ("./**", (1, 0, 0)),
        (".letscode/plans/**", (1, 2, 16)),
        ("secrets/**", (1, 1, 8)),
        ("**/secret", (0, 0, 0)),         # pure-wildcard prefix: least specific
    ])
    def test_value(self, pattern, expected):
        assert _pattern_specificity(pattern) == expected

    def test_more_specific_path_beats_broader(self):
        assert _pattern_specificity("/a/b/*") > _pattern_specificity("/a/*")
        assert _pattern_specificity("/a/*") > _pattern_specificity("/**")
        assert _pattern_specificity(".letscode/plans/**") > _pattern_specificity("/**")

    def test_bare_filename_beats_root_glob(self):
        # A bare filename (depth 1) outranks the root glob (depth 0)
        assert _pattern_specificity("plan.md") > _pattern_specificity("/**")

    def test_pure_wildcard_is_least_specific(self):
        assert _pattern_specificity("**/x") < _pattern_specificity("/**")


# ---------------------------------------------------------------------------
# Most-specific-wins (the core behavior change)
# ---------------------------------------------------------------------------

class TestMostSpecificWins:
    """A specific allow overrides a broad deny; ties break to deny."""

    def test_specific_allow_beats_broad_deny(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(
            allow_write=[".letscode/plans/**"],
            deny_write=["/**"],
        )
        target = tmp_path / ".letscode" / "plans" / "x.md"
        assert check_write(str(target), rules) is None  # allowed

    def test_broad_allow_does_not_beat_specific_deny(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(
            allow_write=["./**"],
            deny_write=["secrets/**"],
        )
        target = tmp_path / "secrets" / "x"
        assert check_write(str(target), rules) is not None  # denied

    def test_tie_breaks_to_deny(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Identical patterns: equal specificity → deny wins
        rules = Rules(
            allow_write=["/a/*"],
            deny_write=["/a/*"],
        )
        assert check_write("/a/file", rules) is not None

    def test_deny_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(deny_write=["/**"])
        assert check_write(str(tmp_path / "x"), rules) is not None

    def test_allow_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(allow_write=["/**"])
        assert check_write(str(tmp_path / "x"), rules) is None

    def test_secret_path_always_denied_even_with_allow(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(allow_write=["/**"])
        # .env is in the secret baseline
        assert check_write(str(tmp_path / ".env"), rules) is not None

    def test_bare_filename_allow_beats_root_deny(self, tmp_path, monkeypatch):
        # The Plan-card scenario: allow plan.md + deny /**
        monkeypatch.chdir(tmp_path)
        rules = Rules(allow_write=["plan.md"], deny_write=["/**"])
        assert check_write("plan.md", rules) is None       # allowed
        assert check_write("src/main.py", rules) is not None  # denied


# ---------------------------------------------------------------------------
# Preset interaction (locks in the builtin Plan-card target scenario)
# ---------------------------------------------------------------------------

class TestPresetInteraction:
    """safe preset + a specific allowWrite now works (the motivating case)."""

    def test_safe_preset_plus_plan_allow(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = merge_rules("safe", Rules(allow_write=["plan.md"]))
        assert check_write("plan.md", rules) is None          # allowed
        assert check_write("src/main.py", rules) is not None  # denied

    def test_safe_preset_plus_nested_allow(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = merge_rules("safe", Rules(allow_write=[".letscode/plans/**"]))
        plans_file = tmp_path / ".letscode" / "plans" / "design.md"
        assert check_write(str(plans_file), rules) is None       # allowed
        assert check_write("src/main.py", rules) is not None     # denied

    def test_default_preset_plus_deny_still_denies(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = merge_rules("default", Rules(deny_write=["secrets/**"]))
        secrets_file = tmp_path / "secrets" / "x"
        assert check_write(str(secrets_file), rules) is not None  # denied


# ---------------------------------------------------------------------------
# check_read parity (regression protection)
# ---------------------------------------------------------------------------

class TestCheckReadParity:
    """check_read keeps default-allow semantics and honors the specificity model."""

    def test_default_allow_no_rules(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert check_read(str(tmp_path / "x"), Rules()) is None

    def test_deny_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(deny_read=["/tmp/**"])
        assert check_read("/tmp/secret", rules) is not None

    def test_specific_allow_beats_broad_deny(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(
            allow_read=["./docs/**"],
            deny_read=["/**"],
        )
        docs_file = tmp_path / "docs" / "readme.md"
        assert check_read(str(docs_file), rules) is None       # allowed
        assert check_read("src/main.py", rules) is not None    # denied

    def test_secret_path_denied_even_with_allow(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = Rules(allow_read=["/**"])
        assert check_read(str(tmp_path / ".aws" / "creds"), rules) is not None
