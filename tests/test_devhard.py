"""Tests for the DevHard multi-agent orchestrator and supporting infrastructure.

Covers:
1. Card structure (DevHard/Worker/Tester roles, tools, presets, hooks)
2. verify.sh — the fixed acceptance contract (run_test.sh existence + execution)
3. devhard_loop.sh — the deterministic Worker-Tester loop controller
4. Agent tool state_file forwarding
5. --state flag plumbing
"""

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from letscode.agent_card import discover_agent_cards, load_agent_card
from letscode.hooks import run_hook
from letscode.tools import agent as agent_tool


# ── Card structure ──

class TestCardStructure:
    def test_all_three_cards_discoverable(self):
        cards = discover_agent_cards()
        assert "devhard" in cards
        assert "worker" in cards
        assert "tester" in cards

    def test_devhard_is_read_only_planner(self):
        """DevHard only does Plan — read-only, has Agent for Plan sub-agent,
        and onAgentEnd drives the Worker-Tester loop."""
        dh = load_agent_card("DevHard")
        assert "Agent" in dh.tools
        assert "Write" not in dh.tools
        assert "Edit" not in dh.tools
        assert dh.preset == "safe"
        assert dh.on_agent_end is not None
        assert "devhard_loop.sh" in dh.on_agent_end

    def test_worker_can_write_but_not_spawn(self):
        w = load_agent_card("Worker")
        assert "Write" in w.tools
        assert "Edit" in w.tools
        assert "Bash" in w.tools
        assert "Agent" not in w.tools  # prevent recursion
        assert w.on_agent_end is None  # pure executor, no hooks

    def test_tester_writes_tests_and_run_test_sh(self):
        """Tester writes test cases + run_test.sh. It needs Write/Edit."""
        t = load_agent_card("Tester")
        assert "Write" in t.tools
        assert "Edit" in t.tools
        assert "Bash" in t.tools
        assert "Read" in t.tools
        assert t.on_agent_end is None


# ── verify.sh — fixed acceptance contract ──

class TestVerifyScript:
    """Test the fixed verify.sh: checks run_test.sh exists, executes it."""

    @pytest.fixture
    def verify_script(self):
        from importlib.resources import files
        p = files("letscode.builtin_agents") / "hooks/verify.sh"
        assert p.is_file(), "verify.sh not found in builtin_agents"
        return str(p)

    def test_run_test_sh_pass_exit_0(self, verify_script, tmp_path):
        """run_test.sh exits 0 → verify.sh exits 0."""
        (tmp_path / "run_test.sh").write_text("#!/bin/bash\nexit 0\n")
        r = run_hook(verify_script, '{}', str(tmp_path), sandbox=False)
        assert r.returncode == 0
        assert "passed" in r.stdout.lower() or "Running" in r.stdout

    def test_run_test_sh_fail_exit_nonzero(self, verify_script, tmp_path):
        """run_test.sh exits 1 → verify.sh exits 1."""
        (tmp_path / "run_test.sh").write_text("#!/bin/bash\necho 'test failed'\nexit 1\n")
        r = run_hook(verify_script, '{}', str(tmp_path), sandbox=False)
        assert r.returncode != 0

    def test_missing_run_test_sh_reports_not_found(self, verify_script, tmp_path):
        """No run_test.sh → verify.sh exits 1 with a clear 'not found' message."""
        r = run_hook(verify_script, '{}', str(tmp_path), sandbox=False)
        assert r.returncode != 0
        assert "not found" in r.stdout.lower()

    def test_run_test_sh_arbitrary_command(self, verify_script, tmp_path):
        """run_test.sh can contain any command (language-agnostic)."""
        (tmp_path / "run_test.sh").write_text("#!/bin/bash\necho 'swift test ok'\nexit 0\n")
        r = run_hook(verify_script, '{}', str(tmp_path), sandbox=False)
        assert r.returncode == 0


# ── devhard_loop.sh — deterministic loop controller ──

class TestDevHardLoopScript:
    """Test the loop controller: spawns Tester+Worker, then verify-fix cycle."""

    @pytest.fixture
    def hook_script(self):
        from importlib.resources import files
        p = files("letscode.builtin_agents") / "hooks/devhard_loop.sh"
        assert p.is_file(), "devhard_loop.sh not found in builtin_agents"
        return str(p)

    @pytest.fixture
    def real_python(self):
        import sys
        return sys.executable

    def _make_fake_python(self, tmp_path, real_python, *, worker_fixes):
        """Create a wrapper script as LETSCODE_PYTHON.

        Intercepts `-m letscode` calls (Tester/Worker spawns):
        - Tester spawn (first time): writes run_test.sh with `exit 1`.
        - Tester spawn (subsequent/review): does NOT touch run_test.sh (simulates
          a review that finds nothing wrong — tests stay as Worker left them).
        - Worker spawn: if worker_fixes, makes run_test.sh pass; else no-op.

        Delegates everything else to real python. Records spawn order via log.
        """
        spawn_log = tmp_path / "spawn_log"
        tester_count_file = tmp_path / "tester_count"
        script = tmp_path / "fakepy.py"

        if worker_fixes:
            worker_action = '  echo "#!/bin/bash\\nexit 0" > run_test.sh\n'
        else:
            worker_action = '  : # Worker does not fix\n'

        script.write_text(
            "#!/bin/bash\n"
            f"REAL={real_python!r}\n"
            'if [ "$1" = "-m" ] && [ "$2" = "letscode" ]; then\n'
            '  CARD=""; for a in "$@"; do [ "$PREV" = "--as" ] && CARD="$a"; PREV="$a"; done\n'
            f'  echo "$CARD" >> "{str(spawn_log)}"\n'
            '  if [ "$CARD" = "Tester" ]; then\n'
            # Only the FIRST Tester spawn writes tests; review spawns leave them.
            f'    TC=$(cat "{str(tester_count_file)}" 2>/dev/null || echo 0)\n'
            '    TC=$((TC + 1))\n'
            f'    echo "$TC" > "{str(tester_count_file)}"\n'
            '    if [ "$TC" = "1" ]; then\n'
            '      echo "#!/bin/bash" > run_test.sh\n'
            '      echo "exit 1" >> run_test.sh\n'
            '    fi\n'
            '  fi\n'
            '  if [ "$CARD" = "Worker" ]; then\n'
            + worker_action +
            '  fi\n'
            '  exit 0\n'
            'fi\n'
            'exec "$REAL" "$@"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script), str(spawn_log)

    def test_missing_config_aborts(self, hook_script, tmp_path, real_python):
        """No LETSCODE_CONFIG → exit 2 (can't spawn sub-agents)."""
        (tmp_path / "plan.md").write_text("# plan\n")
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_PYTHON": real_python})
        assert r.returncode == 2
        assert "LETSCODE_CONFIG" in r.stdout

    def test_loop_passes_when_worker_fixes(self, hook_script, tmp_path, real_python):
        """Full flow: Tester writes tests → Worker fixes → verify passes."""
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        fake_py, spawn_log = self._make_fake_python(
            tmp_path, real_python, worker_fixes=True)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": fake_py})
        assert r.returncode == 0
        assert "All tests passed." in r.stdout
        spawns = Path(spawn_log).read_text().strip().split("\n")
        assert "Tester" in spawns, "Tester was not spawned"
        assert "Worker" in spawns, "Worker was not spawned"

    def test_loop_tester_spawned_before_worker(self, hook_script, tmp_path, real_python):
        """TDD order: Tester is spawned before Worker."""
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        fake_py = tmp_path / "fakepy.py"
        spawn_log = tmp_path / "spawn_log"
        fake_py.write_text(
            "#!/bin/bash\n"
            f'REAL={real_python!r}\n'
            'if [ "$1" = "-m" ] && [ "$2" = "letscode" ]; then\n'
            '  CARD=""; for a in "$@"; do [ "$PREV" = "--as" ] && CARD="$a"; PREV="$a"; done\n'
            f'  echo "$CARD" >> "{str(spawn_log)}"\n'
            '  if [ "$CARD" = "Tester" ]; then echo "#!/bin/bash" > run_test.sh; echo "exit 0" >> run_test.sh; fi\n'
            '  exit 0\n'
            'fi\n'
            'exec "$REAL" "$@"\n'
        )
        fake_py.chmod(fake_py.stat().st_mode | stat.S_IEXEC)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": str(fake_py)})
        assert r.returncode == 0
        order = spawn_log.read_text().strip().split("\n")
        assert order[0] == "Tester", f"Tester should be first, got {order}"
        assert "Worker" in order, f"Worker should be spawned, got {order}"

    def test_loop_max_iterations_exit_2(self, hook_script, tmp_path, real_python):
        """Worker never fixes → max iterations → exit 2."""
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        # Pre-set iteration to max-1 so the loop hits the limit quickly.
        state_file.write_text(json.dumps({"iteration": 4}))
        fake_py, _ = self._make_fake_python(
            tmp_path, real_python, worker_fixes=False)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": fake_py})
        assert r.returncode == 2
        assert "max iterations" in r.stdout.lower()

    def test_loop_state_json_tracks_iteration(self, hook_script, tmp_path, real_python):
        """On failure, state.json records iteration count."""
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        fake_py, _ = self._make_fake_python(
            tmp_path, real_python, worker_fixes=False)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": fake_py})
        # After max iterations, state should have iteration >= 1
        assert state_file.exists(), "state.json not written"
        data = json.loads(state_file.read_text())
        assert data.get("iteration", 0) >= 1

    def test_loop_re_spawns_tester_on_failure(self, hook_script, tmp_path, real_python):
        """On failure, Tester is re-spawned for review BEFORE Worker fixes.

        This is the anti-cheat mechanism: Tester reviews the Worker's changes
        each iteration, ensuring tests aren't tampered with and coverage gaps
        are closed.
        """
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"iteration": 3}))
        fake_py, spawn_log = self._make_fake_python(
            tmp_path, real_python, worker_fixes=False)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": fake_py})
        spawns = Path(spawn_log).read_text().strip().split("\n")
        tester_count = spawns.count("Tester")
        worker_count = spawns.count("Worker")
        assert tester_count >= 2, f"Tester should be re-spawned on failure (got {tester_count} spawns: {spawns})"
        assert worker_count >= 2, f"Worker should be spawned for fix (got {worker_count} spawns: {spawns})"

    def test_loop_anti_cheat_review_on_pass(self, hook_script, tmp_path, real_python):
        """When tests pass, Tester is spawned for a final anti-cheat review."""
        (tmp_path / "plan.md").write_text("# plan\n")
        state_file = tmp_path / "state.json"
        fake_py, spawn_log = self._make_fake_python(
            tmp_path, real_python, worker_fixes=True)
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_STATE": str(state_file),
                          "LETSCODE_CONFIG": "/fake/config.json",
                          "LETSCODE_PYTHON": fake_py})
        assert r.returncode == 0
        spawns = Path(spawn_log).read_text().strip().split("\n")
        tester_count = spawns.count("Tester")
        assert tester_count >= 2, f"Tester should do anti-cheat review after pass (got {tester_count})"


# ── Agent tool: state_file forwarding ──

class TestAgentToolStateForwarding:
    def test_state_file_forwarded(self):
        """When state_file is set, the cmd includes --state <path>."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(stdout="result", stderr="", returncode=0)

        with patch.object(subprocess, "run", side_effect=fake_run):
            agent_tool.execute(
                {"description": "test", "prompt": "do something"},
                state_file="/path/to/state.json",
            )

        assert "--state" in captured_cmd
        idx = captured_cmd.index("--state")
        assert captured_cmd[idx + 1] == "/path/to/state.json"

    def test_no_state_file_no_flag(self):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(stdout="result", stderr="", returncode=0)

        with patch.object(subprocess, "run", side_effect=fake_run):
            agent_tool.execute({"description": "test", "prompt": "do something"})

        assert "--state" not in captured_cmd

    def test_state_forwarded_with_subagent_type(self):
        """State is forwarded even when spawning a card-based sub-agent."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(stdout="result", stderr="", returncode=0)

        with patch.object(subprocess, "run", side_effect=fake_run):
            agent_tool.execute(
                {
                    "description": "test",
                    "prompt": "do something",
                    "subagent_type": "Worker",
                },
                state_file="/tmp/state.json",
                scan_dirs=["/shared"],
            )

        assert "--as" in captured_cmd
        assert "--state" in captured_cmd
        assert "--add-scan-dir" in captured_cmd
