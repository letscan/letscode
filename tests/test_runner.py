"""Unit tests for ToolRunner."""

import asyncio
import pytest

from letscode.rules import Rules
from letscode.tools._types import ToolResult
from letscode.tools.runner import ToolRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


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

    def test_is_file_read_default_false(self):
        runner = ToolRunner([], {}, rules=Rules())
        ir = runner._make_is_file_read()
        assert not ir("/tmp/never_read.txt")

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
