"""Unit tests for ToolRunner."""

import asyncio
import json
import shutil

import pytest

from letscode.rules import Rules
from letscode.tools._types import ToolResult
from letscode.tools.runner import ToolRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously in tests.

    Uses a fresh event loop each call. ``asyncio.get_event_loop()`` is
    deprecated and, once any other test has run ``asyncio.run``, can hand back
    a closed loop — so we create a new one explicitly and close it after.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _collect(runner, name, arguments):
    """Collect all events from ToolRunner.execute."""
    results = []
    async for event in runner.execute(name, arguments):
        results.append(event)
    return results


def _allow_all(access, path):
    return None


# ---------------------------------------------------------------------------
# validate_path tests
# ---------------------------------------------------------------------------

class TestValidatePath:
    """Test the validate_path callback built by ToolRunner."""

    def test_read_allowed(self):
        runner = ToolRunner([], {}, rules=Rules())
        vp = runner._make_validate_path()
        assert vp("read", "/tmp/test.txt") is None

    def test_read_tracks_file(self):
        runner = ToolRunner([], {}, rules=Rules())
        vp = runner._make_validate_path()
        ir = runner._make_is_file_read()

        vp("read", "/tmp/test.txt")
        assert ir("/tmp/test.txt")

    def test_read_denied_by_rule(self):
        runner = ToolRunner([], {}, rules=Rules(deny_read=["/tmp/**"]))
        vp = runner._make_validate_path()
        result = vp("read", "/tmp/secret.txt")
        assert result is not None
        assert "denied" in result

    def test_read_denied_no_tracking(self):
        runner = ToolRunner([], {}, rules=Rules(deny_read=["/tmp/**"]))
        vp = runner._make_validate_path()
        ir = runner._make_is_file_read()

        vp("read", "/tmp/secret.txt")
        assert not ir("/tmp/secret.txt")

    def test_write_allowed(self):
        runner = ToolRunner([], {}, rules=Rules(allow_write=["/**"]))
        vp = runner._make_validate_path()
        assert vp("write", "/tmp/test.txt") is None

    def test_write_denied(self):
        runner = ToolRunner([], {}, rules=Rules(deny_write=["/tmp/**"]))
        vp = runner._make_validate_path()
        result = vp("write", "/tmp/test.txt")
        assert result is not None
        assert "denied" in result

    def test_unknown_access_returns_none(self):
        runner = ToolRunner([], {}, rules=Rules())
        vp = runner._make_validate_path()
        assert vp("execute", "/tmp/test") is None

    def test_edit_before_read_check(self):
        """Edit tool scenario: write check + is_file_read."""
        runner = ToolRunner([], {}, rules=Rules())
        vp = runner._make_validate_path()
        ir = runner._make_is_file_read()

        # File not read yet
        assert not ir("/tmp/edit_test.txt")

        # Read it
        vp("read", "/tmp/edit_test.txt")
        assert ir("/tmp/edit_test.txt")

        # Write check passes
        assert vp("write", "/tmp/edit_test.txt") is None


# ---------------------------------------------------------------------------
# ToolRunner.dispatch tests
# ---------------------------------------------------------------------------

class TestDispatch:
    """Test ToolRunner dispatch logic."""

    def test_unknown_tool(self):
        runner = ToolRunner([], {})
        results = _run(_collect(runner, "UnknownTool", "{}"))
        assert len(results) == 1
        assert results[0].success is False
        assert "Unknown tool" in results[0].content

    def test_invalid_json(self):
        runner = ToolRunner([], {})
        results = _run(_collect(runner, "Bash", "{bad json"))
        assert len(results) == 1
        assert results[0].success is False
        assert "Invalid JSON" in results[0].content

    def test_builtin_executor(self):
        def mock_exec(args, **kwargs):
            return f"executed with {args['input']}"

        runner = ToolRunner(
            [{"function": {"name": "MockTool"}}],
            {"MockTool": mock_exec},
        )
        results = _run(_collect(runner, "MockTool", '{"input": "hello"}'))
        assert len(results) == 1
        assert results[0].success is True
        assert "executed with hello" in results[0].content

    def test_executor_returns_tool_result(self):
        def mock_exec(args, **kwargs):
            return ToolResult(content="done", success=False)

        runner = ToolRunner(
            [{"function": {"name": "MockTool"}}],
            {"MockTool": mock_exec},
        )
        results = _run(_collect(runner, "MockTool", "{}"))
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].content == "done"

    def test_executor_receives_validate_path(self):
        received = {}

        def mock_exec(args, **kwargs):
            received["validate_path"] = kwargs.get("validate_path")
            received["is_file_read"] = kwargs.get("is_file_read")
            return "ok"

        runner = ToolRunner(
            [{"function": {"name": "MockTool"}}],
            {"MockTool": mock_exec},
        )
        _run(_collect(runner, "MockTool", "{}"))
        assert callable(received["validate_path"])
        assert callable(received["is_file_read"])


# ---------------------------------------------------------------------------
# Command deny tests
# ---------------------------------------------------------------------------

class TestCommandDeny:
    """Test coarse-grained command allow/deny at ToolRunner level."""

    def test_bash_cmd_denied(self):
        runner = ToolRunner(
            [], {},
            rules=Rules(deny_cmd=["rm *"]),
        )
        results = _run(_collect(runner, "Bash", '{"command": "rm -rf /"}'))
        assert len(results) == 1
        assert results[0].success is False
        assert "denied" in results[0].content

    def test_bash_cmd_allowed(self):
        def mock_bash(args, **kwargs):
            return ToolResult(content="ok", success=True)

        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": mock_bash},
            rules=Rules(deny_cmd=["rm *"]),
        )
        results = _run(_collect(runner, "Bash", '{"command": "ls -la"}'))
        assert len(results) == 1
        assert results[0].success is True

    def test_bash_receives_sandbox_config(self):
        received = {}

        def mock_bash(args, **kwargs):
            received["preset"] = kwargs.get("preset")
            received["sandbox"] = kwargs.get("sandbox")
            return "ok"

        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": mock_bash},
            preset="safe",
            sandbox=False,
        )
        _run(_collect(runner, "Bash", '{"command": "echo hi"}'))
        assert received["preset"] == "safe"
        assert received["sandbox"] is False


# ---------------------------------------------------------------------------
# MCP dispatch tests
# ---------------------------------------------------------------------------

class TestMcpDispatch:
    """Test MCP tool dispatch."""

    def test_mcp_tool_dispatch(self):
        class FakeMcp:
            async def call_tool(self, name, args):
                return f"mcp result for {name}"

        runner = ToolRunner([], {}, mcp=FakeMcp())
        results = _run(_collect(runner, "mcp__test_tool", '{"arg": "val"}'))
        assert len(results) == 1
        assert results[0].success is True
        assert "mcp result" in results[0].content

    def test_mcp_error(self):
        class FakeMcp:
            async def call_tool(self, name, args):
                return "<error>tool failed</error>"

        runner = ToolRunner([], {}, mcp=FakeMcp())
        results = _run(_collect(runner, "mcp__test_tool", "{}"))
        assert len(results) == 1
        assert results[0].success is False


# ---------------------------------------------------------------------------
# Agent tool dispatch tests
# ---------------------------------------------------------------------------

class TestAgentDispatch:
    """Test agent tool gets its config passed through."""

    def test_agent_receives_config(self):
        received = {}

        def mock_agent(args, **kwargs):
            received.update(kwargs)
            return "agent result"

        runner = ToolRunner(
            [{"function": {"name": "Agent"}}],
            {"Agent": mock_agent},
            agent_config={"config_path": "/tmp/cfg.json", "verbose": True},
        )
        results = _run(_collect(runner, "Agent", '{"description": "test", "prompt": "hello"}'))
        assert len(results) == 1
        assert results[0].success is True
        assert received["config_path"] == "/tmp/cfg.json"
        assert received["verbose"] is True
        # Also gets validate_path and is_file_read
        assert callable(received["validate_path"])


# ---------------------------------------------------------------------------
# definitions property
# ---------------------------------------------------------------------------

class TestDefinitions:
    def test_definitions(self):
        defs = [{"function": {"name": "A"}}, {"function": {"name": "B"}}]
        runner = ToolRunner(defs, {})
        assert runner.definitions == defs

    def test_rules_property(self):
        rules = Rules(deny_write=["/**"])
        runner = ToolRunner([], {}, rules=rules)
        assert runner.rules is rules


# ---------------------------------------------------------------------------
# Denial collection (passive permission escalation)
# ---------------------------------------------------------------------------

class TestDenialCollection:
    """ToolRunner.denials accumulates structured {type, target} records from
    check_cmd / check_read / check_write denials.

    ``last_call_denied`` is owned by agent.py (it compares the denial list
    length before/after each dispatch); the runner only owns the list. Tests
    for last_call_denied live in test_agent_escalation.py."""

    def test_initial_state_empty(self):
        runner = ToolRunner([], {}, rules=Rules())
        assert runner.denials == []
        assert runner.last_call_denied is False

    def test_bash_denial_recorded(self):
        runner = ToolRunner(
            [], {}, rules=Rules(deny_cmd=["rm *"]),
        )
        _run(_collect(runner, "Bash", '{"command": "rm -rf /tmp"}'))
        assert runner.denials == [{"type": "cmd", "target": "rm -rf /tmp"}]

    def test_bash_allowed_no_denial(self):
        def mock_bash(args, **kwargs):
            return ToolResult(content="ok", success=True)
        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": mock_bash},
            rules=Rules(),
        )
        _run(_collect(runner, "Bash", '{"command": "ls"}'))
        assert runner.denials == []

    def test_write_denial_via_validate_path(self):
        runner = ToolRunner(
            [], {}, rules=Rules(deny_write=["/**"]),
        )
        vp = runner._make_validate_path()
        err = vp("write", "/etc/hosts")
        assert err is not None
        assert runner.denials == [{"type": "write", "target": "/etc/hosts"}]

    def test_read_denial_via_validate_path(self):
        runner = ToolRunner(
            [], {}, rules=Rules(deny_read=["/tmp/**"]),
        )
        vp = runner._make_validate_path()
        err = vp("read", "/tmp/secret")
        assert err is not None
        assert runner.denials == [{"type": "read", "target": "/tmp/secret"}]

    def test_read_allowed_no_denial_recorded(self):
        runner = ToolRunner([], {}, rules=Rules())
        vp = runner._make_validate_path()
        err = vp("read", "/tmp/safe")
        assert err is None
        assert runner.denials == []

    def test_denials_accumulate_across_calls(self):
        """Multiple denied calls accumulate records in order."""
        runner = ToolRunner(
            [], {}, rules=Rules(deny_cmd=["rm *", "dd *"]),
        )
        _run(_collect(runner, "Bash", '{"command": "rm -rf /a"}'))
        _run(_collect(runner, "Bash", '{"command": "dd if=/dev/zero"}'))
        assert runner.denials == [
            {"type": "cmd", "target": "rm -rf /a"},
            {"type": "cmd", "target": "dd if=/dev/zero"},
        ]


# ---------------------------------------------------------------------------
# Sandbox-intercept denial detection (runtime heuristic)
# ---------------------------------------------------------------------------

class TestSandboxDenialDetection:
    """When a Bash command's output indicates a permission denial
    (EPERM/EACCES/"Operation not permitted" in stdout+stderr), the failure is
    recorded as a denial so the escalation trigger can fire. Detection keys on
    output keywords INDEPENDENT of exit code — a command chain like
    ``denied_write; echo done`` exits 0 while the denial text is still present.

    These tests run only on macOS where sandbox-exec is available; on other
    platforms the sandbox path is inactive (no sandbox-exec → no wrap → no
    runtime intercept possible), so there's nothing to test here.
    """

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="requires macOS sandbox-exec",
    )
    def test_sandbox_blocked_write_recorded(self, tmp_path):
        # safe preset: writes are denied everywhere. Writing to a short system
        # path the agent doesn't own → EPERM in stderr.
        # (Use a short path so the denial target — truncated to 80 chars —
        # still contains the identifying substring.)
        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": _real_bash_executor},
            preset="safe", sandbox=True,
        )
        results = _run(_collect(
            runner, "Bash",
            json.dumps({"command": "echo x > /tmp/letscode_deny_marker"}),
        ))
        # And a denial was recorded with the command as the target.
        assert any(
            d.get("type") == "cmd" and "letscode_deny_marker" in d.get("target", "")
            for d in runner.denials
        )

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="requires macOS sandbox-exec",
    )
    def test_denied_write_masked_by_trailing_echo_still_recorded(self, tmp_path):
        """The regression from the LetsBot bug report: the agent issues a
        chain whose denied write is masked by a trailing ``echo "Exit code:
        $?"``. The chain exits 0 (the echo succeeds), but the output still
        contains "operation not permitted". Must be recorded regardless of
        the overall exit code."""
        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": _real_bash_executor},
            preset="safe", sandbox=True,
        )
        # Write to ~/ (denied under safe preset), then echo masks the exit.
        cmd = (
            'echo test > ~/letscode_masked_deny 2>&1; '
            'echo "Exit code: $?"'
        )
        _run(_collect(runner, "Bash", json.dumps({"command": cmd})))
        # Denial recorded even though the chain exits 0.
        assert any(
            d.get("type") == "cmd" and "letscode_masked_deny" in d.get("target", "")
            for d in runner.denials
        )

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="requires macOS sandbox-exec",
    )
    def test_sandbox_allowed_command_not_recorded(self, tmp_path):
        # A command that succeeds under the sandbox with no denial keyword in
        # its output must not be flagged.
        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": _real_bash_executor},
            preset="default", sandbox=True,
        )
        _run(_collect(runner, "Bash", json.dumps({"command": "echo hello"})))
        assert runner.denials == []

    @pytest.mark.skipif(
        not shutil.which("sandbox-exec"),
        reason="requires macOS sandbox-exec",
    )
    def test_ordinary_failure_not_recorded(self, tmp_path):
        # A non-sandbox failure (exit 127 command not found) with no denial
        # keyword must NOT be recorded as a denial.
        runner = ToolRunner(
            [{"function": {"name": "Bash"}}],
            {"Bash": _real_bash_executor},
            preset="default", sandbox=True,
        )
        _run(_collect(runner, "Bash",
                      json.dumps({"command": "this_cmd_does_not_exist_xyz"})))
        assert runner.denials == []


async def _real_bash_executor(args, **kwargs):
    """Real bash.execute passthrough for sandbox integration tests."""
    from letscode.tools.bash import execute as _bash_execute
    async for event in _bash_execute(args, **kwargs):
        yield event
