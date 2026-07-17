"""Tests for letscode.acp.permission — passive permission escalation helpers.

Covers:
- generalize_target: deterministic allow-always pattern derivation
- session allow-config file read/write
- probe_permission_request: LLM probe (mocked) extracts precise permission
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from letscode.acp.permission import (
    REQUEST_PERMISSION_SCHEMA,
    add_session_allow_pattern,
    generalize_target,
    load_session_allow_config,
    probe_permission_request,
)
from letscode.stream import StreamResult, ToolCall


# ---------------------------------------------------------------------------
# generalize_target
# ---------------------------------------------------------------------------

class TestGeneralizeCmd:
    def test_known_subcommand_generalizes_to_sub_level(self):
        assert generalize_target("cmd", "npm run dev") == "npm run *"
        assert generalize_target("cmd", "git push origin") == "git push *"
        assert generalize_target("cmd", "cargo build --release") == "cargo build *"

    def test_unknown_command_generalizes_to_name(self):
        assert generalize_target("cmd", "make build") == "make *"
        assert generalize_target("cmd", "curl https://example.com") == "curl *"

    def test_single_token_no_generalization(self):
        assert generalize_target("cmd", "ls") == "ls"
        assert generalize_target("cmd", "pwd") == "pwd"

    def test_quoted_command_parsed(self):
        # shlex handles quotes; "git commit -m 'foo bar'" → "git commit *"
        assert generalize_target("cmd", "git commit -m 'foo bar'") == "git commit *"

    def test_unbalanced_quotes_falls_back(self):
        # ValueError from shlex — falls back to whitespace split.
        result = generalize_target("cmd", "echo 'unbalanced")
        # Either path lands on "echo *"
        assert result == "echo *"


class TestGeneralizePath:
    def test_absolute_path_strips_last_segment(self):
        assert generalize_target("write", "/a/b/c.txt") == "/a/b/*"
        assert generalize_target("read", "/etc/hosts") == "/etc/*"

    def test_relative_path(self):
        assert generalize_target("write", "./src/x.py") == "./src/*"

    def test_bare_filename_no_generalization(self):
        assert generalize_target("write", "plan.md") == "plan.md"

    def test_root_level_falls_back_to_exact(self):
        # /a → parent is "" (root) → too broad → return exact.
        assert generalize_target("write", "/a") == "/a"

    def test_trailing_slash_handled(self):
        assert generalize_target("write", "/a/b/") == "/a/*"

    def test_too_broad_falls_back(self):
        # /a/* is fine; but if we somehow derive "/**" or "*" or "/", fall back.
        # Direct case: a path whose parent is root → can't generalize safely.
        assert generalize_target("write", "/x.txt") == "/x.txt"


class TestGeneralizeUnknownType:
    def test_unknown_type_returns_target_verbatim(self):
        assert generalize_target("other", "whatever") == "whatever"


# ---------------------------------------------------------------------------
# Session allow-config file
# ---------------------------------------------------------------------------

class TestSessionAllowConfig:
    def test_load_missing_file_returns_empty(self, tmp_path):
        assert load_session_allow_config(str(tmp_path), "sid1") == {}

    def test_load_empty_returns_empty(self, tmp_path):
        # Write an empty-ish valid json.
        (tmp_path / ".letscode").mkdir()
        (tmp_path / ".letscode" / "config.sid1.json").write_text("{}")
        assert load_session_allow_config(str(tmp_path), "sid1") == {}

    def test_load_returns_known_keys(self, tmp_path):
        (tmp_path / ".letscode").mkdir()
        cfg = {"allowWrite": ["/a/*"], "allowCmd": ["npm run *"]}
        (tmp_path / ".letscode" / "config.sid1.json").write_text(json.dumps(cfg))
        loaded = load_session_allow_config(str(tmp_path), "sid1")
        assert loaded == {"allowWrite": ["/a/*"], "allowCmd": ["npm run *"]}

    def test_load_ignores_unknown_keys(self, tmp_path):
        (tmp_path / ".letscode").mkdir()
        cfg = {"allowWrite": ["/a/*"], "unknownKey": ["x"], "allowRead": "not-a-list"}
        (tmp_path / ".letscode" / "config.sid1.json").write_text(json.dumps(cfg))
        loaded = load_session_allow_config(str(tmp_path), "sid1")
        assert loaded == {"allowWrite": ["/a/*"]}

    def test_load_malformed_json_returns_empty(self, tmp_path):
        (tmp_path / ".letscode").mkdir()
        (tmp_path / ".letscode" / "config.sid1.json").write_text("not json")
        assert load_session_allow_config(str(tmp_path), "sid1") == {}

    def test_add_creates_file(self, tmp_path):
        add_session_allow_pattern(str(tmp_path), "sid1", "cmd", "npm run *")
        path = tmp_path / ".letscode" / "config.sid1.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data == {"allowCmd": ["npm run *"]}

    def test_add_appends_to_existing_key(self, tmp_path):
        add_session_allow_pattern(str(tmp_path), "sid1", "write", "/a/*")
        add_session_allow_pattern(str(tmp_path), "sid1", "write", "/b/*")
        data = json.loads((tmp_path / ".letscode" / "config.sid1.json").read_text())
        assert data == {"allowWrite": ["/a/*", "/b/*"]}

    def test_add_idempotent(self, tmp_path):
        add_session_allow_pattern(str(tmp_path), "sid1", "cmd", "npm *")
        add_session_allow_pattern(str(tmp_path), "sid1", "cmd", "npm *")
        data = json.loads((tmp_path / ".letscode" / "config.sid1.json").read_text())
        assert data == {"allowCmd": ["npm *"]}

    def test_add_multiple_keys(self, tmp_path):
        add_session_allow_pattern(str(tmp_path), "sid1", "cmd", "npm *")
        add_session_allow_pattern(str(tmp_path), "sid1", "write", "/a/*")
        add_session_allow_pattern(str(tmp_path), "sid1", "read", "/etc/*")
        data = json.loads((tmp_path / ".letscode" / "config.sid1.json").read_text())
        assert data == {
            "allowCmd": ["npm *"],
            "allowWrite": ["/a/*"],
            "allowRead": ["/etc/*"],
        }

    def test_add_unknown_type_noop(self, tmp_path):
        add_session_allow_pattern(str(tmp_path), "sid1", "bogus", "x")
        # No file written for an unknown type.
        assert not (tmp_path / ".letscode" / "config.sid1.json").exists()


# ---------------------------------------------------------------------------
# probe_permission_request
# ---------------------------------------------------------------------------

def _probe_response(tool_call: dict | None):
    """Build a StreamResult carrying 0 or 1 tool_calls matching the probe schema."""
    if tool_call is None:
        return StreamResult(text_content="task not blocked", tool_calls=[])
    tc = ToolCall(
        id="1", name="RequestPermission",
        arguments=json.dumps(tool_call),
    )
    return StreamResult(text_content="", tool_calls=[tc])


class TestProbePermissionRequest:
    def _config_file(self, tmp_path):
        cfg = {"default_model": "m1", "providers": {"p": {
            "base_url": "http://x", "api_key": "k",
            "models": [{"model": "m1"}],
        }}}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    def test_returns_request_when_tool_called(self, tmp_path):
        config_path = self._config_file(tmp_path)
        desired = {"reason": "need to edit hosts", "permission": {
            "type": "write", "target": "/etc/hosts",
        }}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(desired)
            )) as m:
                req = await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "User: edit hosts\nAssistant: I tried...",
                    config_path=config_path,
                )
                # tools schema forwarded
                assert m.call_args.kwargs.get("tools") == [REQUEST_PERMISSION_SCHEMA]
            return req
        req = asyncio.run(run())
        assert req == {
            "type": "write", "target": "/etc/hosts",
            "reason": "need to edit hosts",
        }

    def test_returns_none_when_no_tool_call(self, tmp_path):
        config_path = self._config_file(tmp_path)
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(None)
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "transcript...", config_path=config_path,
                )
        assert asyncio.run(run()) is None

    def test_returns_none_when_tool_args_missing_fields(self, tmp_path):
        config_path = self._config_file(tmp_path)
        # Missing target → invalid, treated as no-request.
        bad = {"reason": "x", "permission": {"type": "write"}}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(bad)
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "transcript...", config_path=config_path,
                )
        assert asyncio.run(run()) is None

    def test_returns_none_on_call_exception(self, tmp_path):
        config_path = self._config_file(tmp_path)
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                side_effect=RuntimeError("network down")
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "transcript...", config_path=config_path,
                )
        assert asyncio.run(run()) is None

    def test_ignores_unrelated_tool_calls(self, tmp_path):
        config_path = self._config_file(tmp_path)
        # A tool call with a different name is ignored → None.
        tc = ToolCall(id="2", name="OtherTool", arguments="{}")
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="", tool_calls=[tc])
            )):
                return await probe_permission_request(
                    [{"type": "cmd", "target": "npm run dev"}],
                    "transcript...", config_path=config_path,
                )
        assert asyncio.run(run()) is None

    def test_transcript_and_denials_in_prompt(self, tmp_path):
        config_path = self._config_file(tmp_path)
        captured = {}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(None)
            )) as m:
                await probe_permission_request(
                    [{"type": "cmd", "target": "npm run dev"}],
                    "THE TRANSCRIPT TEXT",
                    config_path=config_path,
                )
                captured["blocks"] = m.call_args.args[0]
        asyncio.run(run())
        text = captured["blocks"][0]["text"]
        assert "THE TRANSCRIPT TEXT" in text
        assert "npm run dev" in text
