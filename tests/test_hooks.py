"""Tests for the onAgentStart/onAgentEnd hook mechanism.

Covers three layers:
1. AgentCard parsing — frontmatter → fields
2. apply_card — threading into CardOverrides
3. hooks.run_hook — execution, stdin, skipping, exit codes
"""

import json
import os
import stat

import pytest

from letscode.agent_card import _parse_card, apply_card
from letscode.config import ModelConfig
from letscode.hooks import HookResult, run_hook, serialize_run


# ── AgentCard parsing ──

class TestCardParseHooks:
    def test_parses_on_agent_start(self):
        card = _parse_card(
            "---\nname: T\nonAgentStart: ./pre.sh\n---\nbody"
        )
        assert card.on_agent_start == "./pre.sh"

    def test_parses_on_agent_end(self):
        card = _parse_card(
            "---\nname: T\nonAgentEnd: ./post.sh\n---\nbody"
        )
        assert card.on_agent_end == "./post.sh"

    def test_parses_both(self):
        card = _parse_card(
            "---\nname: T\nonAgentStart: ./pre.sh\nonAgentEnd: ./post.sh\n---\nbody"
        )
        assert card.on_agent_start == "./pre.sh"
        assert card.on_agent_end == "./post.sh"

    def test_absent_fields_are_none(self):
        card = _parse_card("---\nname: T\n---\nbody")
        assert card.on_agent_start is None
        assert card.on_agent_end is None

    def test_non_string_ignored(self):
        card = _parse_card("---\nname: T\nonAgentStart: [1,2]\n---\nbody")
        assert card.on_agent_start is None


class TestApplyCardHooks:
    def test_card_with_hooks_threaded(self):
        card = _parse_card(
            "---\nname: T\nonAgentStart: ./pre.sh\nonAgentEnd: ./post.sh\n---\nbody"
        )
        cfg = ModelConfig(model="m")
        ov = apply_card(cfg, {}, card)
        assert ov.on_agent_start == "./pre.sh"
        assert ov.on_agent_end == "./post.sh"

    def test_none_card_gives_none(self):
        cfg = ModelConfig(model="m")
        ov = apply_card(cfg, {}, None)
        assert ov.on_agent_start is None
        assert ov.on_agent_end is None


# ── Hook execution ──

def _make_executable(tmp_path, name, content):
    """Write a shell script and make it executable."""
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + content)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class TestRunHook:
    def test_executes_and_captures_stdout(self, tmp_path):
        path = _make_executable(tmp_path, "hook.sh", 'echo "hello world"')
        r = run_hook(path, "{}", str(tmp_path), sandbox=False)
        assert r.returncode == 0
        assert "hello world" in r.stdout
        assert not r.skipped

    def test_receives_stdin(self, tmp_path):
        path = _make_executable(tmp_path, "hook.sh", 'cat')
        stdin_data = json.dumps({"turn": 5, "tool_calls": [{"name": "Bash", "success": True}]})
        r = run_hook(path, stdin_data, str(tmp_path), sandbox=False)
        assert r.returncode == 0
        assert json.loads(r.stdout)["turn"] == 5

    def test_nonexistent_path_skipped(self, tmp_path):
        r = run_hook("./nonexistent.sh", "{}", str(tmp_path), sandbox=False)
        assert r.skipped is True
        assert r.returncode == 0
        assert r.stdout == ""

    def test_relative_path_resolved(self, tmp_path):
        _make_executable(tmp_path, "hook.sh", 'echo "relative works"')
        # Pass relative path; run_hook resolves against cwd.
        r = run_hook("hook.sh", "{}", str(tmp_path), sandbox=False)
        assert r.returncode == 0
        assert "relative works" in r.stdout

    def test_nonzero_exit_captured(self, tmp_path):
        path = _make_executable(tmp_path, "hook.sh", 'echo "oops" >&2; exit 1')
        r = run_hook(path, "{}", str(tmp_path), sandbox=False)
        assert r.returncode == 1

    def test_abort_exit_code_2(self, tmp_path):
        """Exit code 2 = explicit abort. stdout is the error message."""
        path = _make_executable(tmp_path, "hook.sh", 'echo "prerequisites not met"; exit 2')
        r = run_hook(path, "{}", str(tmp_path), sandbox=False)
        assert r.returncode == 2
        assert r.aborted is True
        assert r.ok is False
        assert "prerequisites not met" in r.stdout

    def test_ok_property(self, tmp_path):
        """Exit code 0 with output → ok=True."""
        path = _make_executable(tmp_path, "hook.sh", 'echo "ready"')
        r = run_hook(path, "{}", str(tmp_path), sandbox=False)
        assert r.returncode == 0
        assert r.ok is True
        assert r.aborted is False

    def test_timeout_returns_124(self, tmp_path):
        path = _make_executable(tmp_path, "hook.sh", 'sleep 10')
        r = run_hook(path, "{}", str(tmp_path), sandbox=False, timeout=1)
        assert r.returncode == 124


# ── serialize_run ──

class TestSerializeRun:
    def test_round_trips_json(self):
        tool_calls = [
            {"name": "Agent", "success": True},
            {"name": "Bash", "success": False},
        ]
        s = serialize_run(7, tool_calls)
        d = json.loads(s)
        assert d["turn"] == 7
        assert len(d["tool_calls"]) == 2

    def test_empty_for_on_agent_start(self):
        s = serialize_run(0, [])
        d = json.loads(s)
        assert d["turn"] == 0
        assert d["tool_calls"] == []
