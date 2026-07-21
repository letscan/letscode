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
        desired = {"reason": "need to edit hosts", "permissions": [
            {"type": "write", "target": "/etc/hosts"},
        ]}
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
            "permissions": [{"type": "write", "target": "/etc/hosts"}],
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

    def test_returns_none_when_permissions_empty_or_invalid(self, tmp_path):
        config_path = self._config_file(tmp_path)
        # permissions missing target → all filtered out → None.
        bad = {"reason": "x", "permissions": [{"type": "write"}]}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(bad)
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "transcript...", config_path=config_path,
                )
        assert asyncio.run(run()) is None

    def test_deduplicates_permissions(self, tmp_path):
        """The probe post-processes the LLM output to dedupe by (type, target)
        — the LLM may return the same target twice when confused. Order is
        preserved (first occurrence wins). Note: cmd permissions with
        extractable paths are normalized to write permissions (see
        normalize_permissions), so `rm /b` becomes `write: /b`."""
        config_path = self._config_file(tmp_path)
        raw = {"reason": "r", "permissions": [
            {"type": "write", "target": "/a"},
            {"type": "write", "target": "/a"},   # dup
            {"type": "cmd", "target": "rm /b"},  # → write: /b
            {"type": "write", "target": "/a"},   # dup
        ]}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(raw)
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/a"}],
                    "transcript...", config_path=config_path,
                )
        req = asyncio.run(run())
        assert req["permissions"] == [
            {"type": "write", "target": "/a"},
            {"type": "write", "target": "/b"},
        ]

    def test_backward_compat_single_permission_field(self, tmp_path):
        """Older probe outputs returned a single `permission` object instead of
        a `permissions` list. The parser accepts it as a one-element list so a
        transient LLM regression to the old shape doesn't break the flow."""
        config_path = self._config_file(tmp_path)
        legacy = {"reason": "r", "permission": {
            "type": "write", "target": "/etc/hosts",
        }}
        async def run():
            with patch("letscode.acp.permission.call_llm", new=AsyncMock(
                return_value=_probe_response(legacy)
            )):
                return await probe_permission_request(
                    [{"type": "write", "target": "/etc/hosts"}],
                    "transcript...", config_path=config_path,
                )
        req = asyncio.run(run())
        assert req == {
            "permissions": [{"type": "write", "target": "/etc/hosts"}],
            "reason": "r",
        }

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

    def test_system_prompt_two_stage_structure(self):
        """Lock in the probe's two-stage framing so a future edit can't
        silently collapse them or widen STAGE 1's scope back into the
        LetsBot delete-scenario failure mode (agent tried 13 methods, all
        denied, gave up → probe read 'gave up' as 'non-permission issue').

        STAGE 1 = decide whether to give a retry at all (suppress ONLY if
        the agent completed the task despite denials). STAGE 2 = if retrying,
        extract the MINIMAL permission set for ONE retry.

        Asserts on concept keywords (case-insensitive) so reasonable rewording
        survives, but a structural rewrite doesn't."""
        from letscode.acp.permission import _PROBE_SYSTEM_PROMPT
        p = _PROBE_SYSTEM_PROMPT.lower()
        # Two stages are explicitly named and ordered.
        assert "stage 1" in p and "stage 2" in p
        # STAGE 1 is about whether to retry, gated on task completion.
        assert "retry" in p
        assert "did the agent finish what the user asked for" in p
        # STAGE 1's "not completion" patterns (the delete-scenario shapes).
        assert "rm, mv, chmod" in p
        assert "你需要手动执行" in p
        # STAGE 2 is about the minimal permission set for ONE retry.
        assert "minimal" in p and "one retry" in p
        # Default bias: unsure → do not suppress (let the popup through).
        assert "do not suppress" in p or "let the popup through" in p


# ---------------------------------------------------------------------------
# Path-target extraction (cmd denial → path-level permission)
# ---------------------------------------------------------------------------

class TestExtractPathTargets:
    """Deterministic extraction of file-path operands from denied commands.

    Replaces an unstable LLM extraction: the probe would sometimes return
    `rm a b && echo done` as a single verbatim "target" that matches nothing
    when the agent retries (allow-cmd matches the exact string). The right
    unit for path-operating commands is the PATH (a write-rule), since the
    sandbox blocks at the file-write syscall level regardless of command."""

    def test_rm_multiple_paths(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets("rm ../.git ../../.git") == ["../.git", "../../.git"]

    def test_rm_with_flags_skipped(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets("rm -rf /tmp/x") == ["/tmp/x"]

    def test_compound_command_splits_on_chain(self):
        from letscode.acp.permission import extract_path_targets
        # The `&& echo done && ls` segments are diagnostic — only rm's paths mined.
        result = extract_path_targets(
            'rm ../.git ../../.git && echo "done" && ls -la ../.git'
        )
        assert result == ["../.git", "../../.git"]

    def test_sudo_prefix_stripped(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets("sudo rm ../.git") == ["../.git"]
        assert extract_path_targets("doas rm ../.git") == ["../.git"]

    def test_redirection_target_extracted(self):
        from letscode.acp.permission import extract_path_targets
        # `: > file` and `echo x > file` — the path is the redirect target.
        assert extract_path_targets(": > ../.git") == ["../.git"]
        assert extract_path_targets("echo x > ../file") == ["../file"]
        assert extract_path_targets("cat input >> ../log") == ["../log"]

    def test_cp_both_paths(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets("cp /tmp/a ../b") == ["/tmp/a", "../b"]

    def test_deduplicates_preserving_order(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets("rm /a /b /a /c /b") == ["/a", "/b", "/c"]

    def test_non_path_command_returns_empty(self):
        from letscode.acp.permission import extract_path_targets
        # These fall back to the full command string as the permission unit.
        assert extract_path_targets("npm run dev") == []
        assert extract_path_targets("curl https://example.com") == []
        assert extract_path_targets("git status") == []

    def test_diagnostic_segments_not_mined(self):
        from letscode.acp.permission import extract_path_targets
        # `ls -la ../.git` is a read/diagnostic — but ls is not in
        # _PATH_OPERATING_COMMANDS, so ../.git from that segment is skipped.
        # Only the rm segment contributes.
        result = extract_path_targets("rm /target && ls -la /diagnostic")
        assert result == ["/target"]

    def test_relative_dot_path(self):
        from letscode.acp.permission import extract_path_targets
        # `.` alone is cwd — treated as a path.
        assert "." in extract_path_targets("chmod 755 .")

    def test_quoted_path_handled(self):
        from letscode.acp.permission import extract_path_targets
        assert extract_path_targets('rm "my file.txt"') == []

    def test_real_session_delete_command(self):
        """The exact command from the LetsBot bug report (11 denials)."""
        from letscode.acp.permission import extract_path_targets
        cmd = ('rm ../.git ../../.git && echo "删除成功" && '
               'ls -la ../.git 2>&1; ls -la ../../.git 2>&1')
        assert extract_path_targets(cmd) == ["../.git", "../../.git"]


class TestNormalizePermissions:
    """probe output → path-level permissions (the post-LLM deterministic pass)."""

    def test_cmd_with_paths_becomes_write_permissions(self):
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions([
            {"type": "cmd", "target": "rm ../.git ../../.git"},
        ])
        assert out == [
            {"type": "write", "target": "../.git"},
            {"type": "write", "target": "../../.git"},
        ]

    def test_cmd_without_paths_falls_back_to_cmd(self):
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions([
            {"type": "cmd", "target": "npm run dev"},
        ])
        assert out == [{"type": "cmd", "target": "npm run dev"}]

    def test_write_permission_passes_through(self):
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions([
            {"type": "write", "target": "/etc/hosts"},
        ])
        assert out == [{"type": "write", "target": "/etc/hosts"}]

    def test_mixed_cmd_and_write_dedup_across_types(self):
        """A cmd `rm /a` and a write `/a` both normalize to write:/a — deduped."""
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions([
            {"type": "cmd", "target": "rm /a"},
            {"type": "write", "target": "/a"},
        ])
        assert out == [{"type": "write", "target": "/a"}]

    def test_empty_input(self):
        from letscode.acp.permission import normalize_permissions
        assert normalize_permissions([]) == []


class TestNormalizeAbsoluteResolution:
    """When cwd is provided, path targets are resolved to absolute form so
    the allow-rule matches regardless of the agent's path form on retry.
    This collapses the LLM's path-form instability (../.git vs /abs/.git)."""

    def test_relative_path_resolved_to_absolute(self, tmp_path):
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions(
            [{"type": "cmd", "target": "rm ../.git"}],
            cwd=str(tmp_path / "workspace"),
        )
        expected = str((tmp_path / "workspace" / "../.git").resolve())
        assert out == [{"type": "write", "target": expected}]

    def test_absolute_path_passes_through_realpath(self, tmp_path):
        from letscode.acp.permission import normalize_permissions
        # An already-absolute path is normalized via realpath (symlinks resolved)
        # but stays absolute.
        out = normalize_permissions(
            [{"type": "write", "target": str(tmp_path / "x.txt")}],
            cwd=str(tmp_path),
        )
        assert out == [{"type": "write", "target": str((tmp_path / "x.txt").resolve())}]

    def test_relative_and_absolute_collapse_to_same(self, tmp_path):
        """The key stability property: `../.git` and its absolute equivalent
        produce the SAME normalized target, so the dedup merges them."""
        from letscode.acp.permission import normalize_permissions
        ws = tmp_path / "workspace"
        ws.mkdir()
        abs_git = str((ws / "../.git").resolve())
        # Two perms: one relative, one absolute — same file.
        out = normalize_permissions(
            [
                {"type": "cmd", "target": "rm ../.git"},          # relative
                {"type": "write", "target": abs_git},              # absolute
            ],
            cwd=str(ws),
        )
        # Deduped to one entry (both resolve to the same absolute path).
        assert len(out) == 1
        assert out[0] == {"type": "write", "target": abs_git}

    def test_no_cwd_keeps_relative_verbatim(self):
        """Without cwd, paths are NOT resolved — backward compat for callers
        that don't have a session cwd (e.g. the unit-test path)."""
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions(
            [{"type": "cmd", "target": "rm ../.git"}],
        )
        assert out == [{"type": "write", "target": "../.git"}]

    def test_tilde_expanded(self, tmp_path):
        from letscode.acp.permission import normalize_permissions
        out = normalize_permissions(
            [{"type": "cmd", "target": "rm ~/.gitconfig"}],
            cwd=str(tmp_path),
        )
        import os
        assert out == [{"type": "write", "target": os.path.realpath(os.path.expanduser("~/.gitconfig"))}]

    def test_cmd_without_paths_not_affected_by_cwd(self, tmp_path):
        from letscode.acp.permission import normalize_permissions
        # Fallback path: cmd with no extractable paths stays as cmd verbatim.
        out = normalize_permissions(
            [{"type": "cmd", "target": "npm run dev"}],
            cwd=str(tmp_path),
        )
        assert out == [{"type": "cmd", "target": "npm run dev"}]
