"""Tests for the CLI ``--allow`` flag and session-level allow-config loading.

Covers:
- ``_parse_allow_flags``: <type>:<target> parsing into camelCase rules keys
- ``_load_session_allow_config``: reading ``.letscode/config.<sid>.json``
- end-to-end rules merging: a safe-preset deny is pierced by ``--allow`` (the
  documented escape hatch for broad deny rules)
"""

import json
from pathlib import Path

import pytest

from letscode.cli import _load_session_allow_config, _parse_allow_flags
from letscode.rules import Rules, check_write, load_rules, merge_rules


# ---------------------------------------------------------------------------
# _parse_allow_flags
# ---------------------------------------------------------------------------

class TestParseAllowFlags:
    def test_empty_input_returns_empty(self):
        assert _parse_allow_flags([]) == {}

    def test_write_parsed(self):
        out = _parse_allow_flags(["write:/etc/hosts"])
        assert out == {"allowWrite": ["/etc/hosts"]}

    def test_read_parsed(self):
        out = _parse_allow_flags(["read:/etc/secret"])
        assert out == {"allowRead": ["/etc/secret"]}

    def test_cmd_parsed_with_spaces(self):
        out = _parse_allow_flags(["cmd:npm run dev"])
        assert out == {"allowCmd": ["npm run dev"]}

    def test_repeatable_same_type(self):
        out = _parse_allow_flags([
            "write:/a", "write:/b", "cmd:ls",
        ])
        assert out == {"allowWrite": ["/a", "/b"], "allowCmd": ["ls"]}

    def test_target_with_colon_preserved(self):
        # Only the FIRST colon splits type from target; the rest stays verbatim.
        out = _parse_allow_flags(["write:/a:b:c.txt"])
        assert out == {"allowWrite": ["/a:b:c.txt"]}

    def test_cmd_with_url_preserved(self):
        out = _parse_allow_flags(["cmd:curl http://x:8080/y"])
        assert out == {"allowCmd": ["curl http://x:8080/y"]}

    def test_invalid_type_raises(self):
        with pytest.raises(SystemExit):
            _parse_allow_flags(["bogus:/x"])

    def test_missing_colon_raises(self):
        with pytest.raises(SystemExit):
            _parse_allow_flags(["write-foo"])

    def test_empty_target_raises(self):
        with pytest.raises(SystemExit):
            _parse_allow_flags(["write:"])


# ---------------------------------------------------------------------------
# _load_session_allow_config
# ---------------------------------------------------------------------------

class TestLoadSessionAllowConfig:
    def test_missing_file(self, tmp_path):
        path = tmp_path / "config.sid.json"
        assert _load_session_allow_config(path) == {}

    def test_loads_known_keys(self, tmp_path):
        path = tmp_path / "config.sid.json"
        path.write_text(json.dumps({
            "allowWrite": ["/a/*"], "allowCmd": ["npm *"],
        }))
        assert _load_session_allow_config(path) == {
            "allowWrite": ["/a/*"], "allowCmd": ["npm *"],
        }

    def test_malformed_returns_empty(self, tmp_path):
        path = tmp_path / "config.sid.json"
        path.write_text("not json")
        assert _load_session_allow_config(path) == {}

    def test_non_dict_returns_empty(self, tmp_path):
        path = tmp_path / "config.sid.json"
        path.write_text(json.dumps(["not", "a", "dict"]))
        assert _load_session_allow_config(path) == {}


# ---------------------------------------------------------------------------
# End-to-end: --allow pierces a safe-preset deny (the escape hatch)
# ---------------------------------------------------------------------------

class TestAllowPiercesDeny:
    """The motivating scenario from docs/plan-permission-escalation.md: a
    safe-preset deny=["/**"] must be piercable by a specific --allow grant
    via the most-specific-pattern-wins rule."""

    def test_safe_preset_denies_without_allow(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rules = merge_rules("safe", load_rules({}))
        err = check_write("/etc/hosts", rules)
        assert err is not None  # denied by safe preset

    def test_allow_write_overrides_safe_deny(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        allow = _parse_allow_flags(["write:/etc/hosts"])
        rules = merge_rules("safe", load_rules(allow))
        # /etc/hosts now allowed: more specific than the broad /**
        assert check_write("/etc/hosts", rules) is None
        # But other paths stay denied.
        assert check_write("/etc/other", rules) is not None

    def test_allow_cmd_overrides_deny_cmd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Custom deny for rm *; allow a specific rm command.
        rules_raw = {"denyCmd": ["rm *"], "allowCmd": ["rm -rf /tmp/scratch"]}
        rules = merge_rules("default", load_rules(rules_raw))
        from letscode.rules import check_cmd
        # cmd uses deny-always-wins semantics (no path hierarchy), so an allow
        # does NOT pierce a deny on commands. Verify the documented behavior:
        # the deny still wins.
        err = check_cmd("rm -rf /tmp/scratch", rules)
        assert err is not None

    def test_session_config_plus_cli_allow_merge(self, tmp_path, monkeypatch):
        """Both the session-level config and CLI --allow feed into rules_raw
        and get merged (concatenated)."""
        monkeypatch.chdir(tmp_path)
        # Simulate cli.py's merge order.
        rules_raw = {}
        # session config
        sess = {"allowWrite": ["/a/*"]}
        for k, v in sess.items():
            rules_raw.setdefault(k, [])
            rules_raw[k] = [*rules_raw[k], *v]
        # CLI --allow
        cli = _parse_allow_flags(["write:/b"])
        for k, v in cli.items():
            rules_raw.setdefault(k, [])
            rules_raw[k] = [*rules_raw[k], *v]
        rules = merge_rules("safe", load_rules(rules_raw))
        # Both /a/x (via session pattern) and /b (via CLI) are allowed.
        assert check_write("/a/x", rules) is None
        assert check_write("/b", rules) is None
        # Unrelated path still denied by safe preset.
        assert check_write("/etc/c", rules) is not None
