"""Tests for AgentCard loading, merging, and ToolRunner whitelist enforcement."""

import asyncio

import pytest

from letscode.agent_card import (
    AgentCard,
    CardOverrides,
    _discover_builtin_cards,
    _merge_rules_raw,
    _parse_card,
    apply_card,
    discover_agent_cards,
    load_agent_card,
    load_builtin_card,
)
from letscode.config import ModelConfig
from letscode.tools.runner import ToolRunner


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestAgentCardParsing:
    """_parse_card turns markdown-with-YAML-frontmatter into an AgentCard."""

    def test_full_frontmatter(self):
        text = (
            "---\n"
            "name: CodeReviewer\n"
            'description: Read-only code review specialist\n'
            "tools: [Read, Grep, Glob, \"mcp__playwright__screenshot\"]\n"
            "skills: [review, lint]\n"
            "mcp_servers: [playwright]\n"
            "rules:\n"
            "  denyWrite: [\"secrets/**\"]\n"
            "  denyCmd: [\"rm *\", \"git push *\"]\n"
            "---\n"
            "You are a code review specialist.\n"
        )
        card = _parse_card(text)
        assert card.name == "CodeReviewer"
        assert card.description == "Read-only code review specialist"
        assert card.tools == ["Read", "Grep", "Glob", "mcp__playwright__screenshot"]
        assert card.skills == ["review", "lint"]
        assert card.mcp_servers == ["playwright"]
        assert card.rules == {"denyWrite": ["secrets/**"], "denyCmd": ["rm *", "git push *"]}
        assert card.body == "You are a code review specialist."

    def test_multiline_body_preserved(self):
        text = (
            "---\n"
            "name: X\n"
            "---\n"
            "# Instructions\n\n"
            "Do things.\n\n"
            "- item one\n"
            "- item two\n"
        )
        card = _parse_card(text)
        assert card.body.startswith("# Instructions")
        assert "- item one" in card.body
        assert "- item two" in card.body

    def test_no_frontmatter(self):
        # No leading ---: entire file is the body (skill-consistent behavior)
        text = "# Just a body\n\nNo frontmatter here."
        card = _parse_card(text)
        assert card.name is None
        assert card.tools is None
        assert card.body == text

    def test_empty_frontmatter(self):
        text = "---\n---\nBody only."
        card = _parse_card(text)
        assert card.name is None
        assert card.tools is None
        assert card.body == "Body only."

    def test_partial_fields(self):
        text = "---\nname: Minimal\ntools: [Read]\n---\nbody"
        card = _parse_card(text)
        assert card.name == "Minimal"
        assert card.tools == ["Read"]
        assert card.skills is None
        assert card.mcp_servers is None
        assert card.rules is None

    def test_non_list_tools_ignored(self):
        # A scalar where a list is expected is ignored, not crashing
        text = "---\nname: X\ntools: Read\n---\nbody"
        card = _parse_card(text)
        assert card.name == "X"
        assert card.tools is None  # not a list → ignored

    def test_closing_delimiter_with_trailing_whitespace(self):
        # "---  " or "---\n" should still close the frontmatter block
        text = "---\nname: X\n---   \nbody"
        card = _parse_card(text)
        assert card.name == "X"
        assert card.body == "body"


# ---------------------------------------------------------------------------
# Discovery & loading
# ---------------------------------------------------------------------------

class TestAgentCardDiscovery:
    """discover_agent_cards scans agents/*.md case-insensitively by stem."""

    def test_discover_finds_md_files(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "Reviewer.md").write_text("---\nname: R\n---\nb")
        (tmp_path / "agents" / "test-runner.md").write_text("---\nname: T\n---\nb")
        # Non-md files are ignored
        (tmp_path / "agents" / "notes.txt").write_text("nope")
        # Subdirectories are not scanned
        (tmp_path / "agents" / "nested").mkdir()
        (tmp_path / "agents" / "nested" / "deep.md").write_text("---\nname: D\n---\nb")

        cards = discover_agent_cards(cwd=str(tmp_path))
        keys = set(cards.keys())
        # Project cards are present
        assert "reviewer" in keys
        assert "test-runner" in keys
        # Builtins are also present (project + builtin coexist)
        assert "explore" in keys
        # Non-md and nested dirs are ignored
        assert "notes" not in keys
        assert "deep" not in keys

    def test_discover_no_project_dir_still_has_builtins(self, tmp_path):
        # With no project agents/, discover returns the builtins (not empty)
        cards = discover_agent_cards(cwd=str(tmp_path))
        assert "explore" in cards
        assert "plan" in cards

    def test_load_case_insensitive(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "CodeReviewer.md").write_text(
            "---\nname: CR\ndescription: x\n---\nbody here"
        )
        card = load_agent_card("codereviewer", cwd=str(tmp_path))
        assert card.name == "CR"
        assert card.body == "body here"

    def test_load_not_found_lists_available(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "Reviewer.md").write_text("---\nname: R\n---\nb")
        (tmp_path / "agents" / "Runner.md").write_text("---\nname: R\n---\nb")
        with pytest.raises(SystemExit) as ei:
            load_agent_card("missing", cwd=str(tmp_path))
        msg = str(ei.value)
        assert "missing" in msg
        # Project cards appear in the available list
        assert "reviewer" in msg
        assert "runner" in msg
        # Builtins appear too (project + builtin coexist)
        assert "explore" in msg

    def test_load_when_no_agents_dir_falls_back_to_builtins(self, tmp_path):
        # No project agents/ dir: builtins still available, so a bogus name
        # reports "not found" listing the builtins rather than "no agents/ dir".
        with pytest.raises(SystemExit) as ei:
            load_agent_card("nonexistent_card", cwd=str(tmp_path))
        msg = str(ei.value)
        assert "nonexistent_card" in msg
        assert "explore" in msg

    def test_project_card_overrides_builtin(self, tmp_path):
        # A project agents/Explore.md takes precedence over the builtin Explore
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "Explore.md").write_text(
            "---\nname: MyExplore\ndescription: custom\n---\nmy body"
        )
        card = load_agent_card("explore", cwd=str(tmp_path))
        assert card.name == "MyExplore"
        assert card.body == "my body"


# ---------------------------------------------------------------------------
# Builtin cards (shipped with the package)
# ---------------------------------------------------------------------------

class TestBuiltinCards:
    """Builtin cards ship with letscode and are loadable via importlib.resources."""

    BUILTIN_NAMES = ["Explore", "Plan", "Review", "SetupZed"]

    def test_discover_builtin_cards_returns_all(self):
        builtins = _discover_builtin_cards()
        keys = set(builtins.keys())
        for name in self.BUILTIN_NAMES:
            assert name.lower() in keys

    def test_load_builtin_card_each(self):
        from importlib.resources import files
        for name in self.BUILTIN_NAMES:
            card = load_builtin_card(name)
            assert card.name == name
            assert card.body  # non-empty prompt
            assert "{{ env }}" in card.body  # all reference the env var

    def test_builtin_card_fields(self):
        explore = load_builtin_card("Explore")
        assert explore.tools == ["Read", "Glob", "Grep", "Agent"]
        # preset is a frontmatter field we don't parse into AgentCard yet — it's
        # in the raw text. Verify it's declared.
        # (preset/tools/skills/rules are the 6 supported fields; preset is read
        # from frontmatter text here as a sanity check on authoring.)
        from importlib.resources import files
        raw = files("letscode.builtin_agents").joinpath("Explore.md").read_text()
        assert "preset: safe" in raw

    def test_plan_card_has_write_rules(self):
        plan = load_builtin_card("Plan")
        # Plan is the only builtin that may write (plan files only)
        assert "Write" in plan.tools
        assert plan.rules is not None
        assert "allowWrite" in plan.rules
        assert "plan.md" in plan.rules["allowWrite"]
        assert ".letscode/plans/**" in plan.rules["allowWrite"]

    def test_setupzed_can_write(self):
        zed = load_builtin_card("SetupZed")
        assert "Write" in zed.tools
        assert "Edit" in zed.tools

    def test_readonly_agents_have_no_write(self):
        for name in ["Explore", "Review"]:
            card = load_builtin_card(name)
            assert "Write" not in (card.tools or [])
            assert "Edit" not in (card.tools or [])

    def test_load_builtin_card_unknown_raises(self):
        with pytest.raises(SystemExit) as ei:
            load_builtin_card("NoSuchBuiltin")
        assert "NoSuchBuiltin" in str(ei.value)

    def test_discover_includes_builtins_without_project_dir(self, tmp_path):
        # With no project agents/, discover still surfaces builtins
        cards = discover_agent_cards(cwd=str(tmp_path))
        assert "explore" in cards
        assert "plan" in cards

    def test_builtin_loads_via_load_agent_card(self, tmp_path):
        # load_agent_card (the --as path) resolves builtins when no project
        # card shadows them.
        card = load_agent_card("review", cwd=str(tmp_path))
        assert card.name == "Review"


# ---------------------------------------------------------------------------
# Card preset: overrides config.preset (CLI --preset still wins)
# ---------------------------------------------------------------------------

class TestCardPreset:
    """A card's `preset` frontmatter field overrides config.json's preset.

    Priority chain: CLI --preset > card.preset > config.preset. The card's
    preset flows through apply_card -> ModelConfig.preset -> merge_rules so the
    rules engine sees the card's intent (e.g. Plan's safe + allowWrite plan.md).
    """

    def test_parse_card_preset(self):
        text = "---\nname: X\npreset: safe\n---\nbody"
        card = _parse_card(text)
        assert card.preset == "safe"

    def test_parse_card_no_preset(self):
        text = "---\nname: X\n---\nbody"
        card = _parse_card(text)
        assert card.preset is None

    def test_apply_card_preset_passthrough(self):
        cfg = ModelConfig(model="m", api_key="k", preset="default")
        card = AgentCard(preset="safe")
        ov = apply_card(cfg, {}, card)
        assert ov.preset == "safe"

    def test_apply_card_no_preset_is_none(self):
        cfg = ModelConfig(model="m", api_key="k", preset="default")
        card = AgentCard(preset=None, body="b")
        ov = apply_card(cfg, {}, card)
        assert ov.preset is None

    def test_no_card_preset_is_none(self):
        cfg = ModelConfig(model="m", api_key="k", preset="default")
        ov = apply_card(cfg, {}, None)
        assert ov.preset is None

    def test_plan_builtin_has_safe_preset(self):
        plan = load_builtin_card("Plan")
        assert plan.preset == "safe"

    def test_plan_card_rules_engine_end_to_end(self, tmp_path, monkeypatch):
        """The motivating case: Plan's preset=safe + allowWrite plan.md paths
        yields write access only to plan files, not source code."""
        from letscode.rules import merge_rules, load_rules, check_write

        monkeypatch.chdir(tmp_path)
        # Simulate cli.py: config.preset=default, then card overrides to safe
        config = ModelConfig(model="m", api_key="k", preset="default")
        card = load_builtin_card("Plan")
        ov = apply_card(config, {}, card)
        config.preset = ov.preset
        rules = merge_rules(config.preset, load_rules(ov.rules_raw))

        assert check_write("plan.md", rules) is None                 # allowed
        assert check_write(".letscode/plans/x.md", rules) is None    # allowed
        assert check_write("src/main.py", rules) is not None         # denied
        assert check_write(".env", rules) is not None                # secrets denied


# ---------------------------------------------------------------------------
# apply_card: the single merge point
# ---------------------------------------------------------------------------

class TestApplyCardMerge:
    """apply_card merges card onto (config, mcp_servers) into CardOverrides."""

    def _config(self, rules=None):
        return ModelConfig(
            model="m", api_key="k", rules=rules,
        )

    def test_no_card_returns_defaults(self):
        cfg = self._config(rules={"allowRead": ["./**"]})
        mcp = {"a": {"url": "x"}, "b": {"command": "y"}}
        ov = apply_card(cfg, mcp, None)
        assert isinstance(ov, CardOverrides)
        assert ov.mcp_servers == mcp                      # unchanged
        assert ov.rules_raw == {"allowRead": ["./**"]}    # config rules
        assert ov.system_prompt is None                   # fall back to built-in
        assert ov.tool_allowlist is None
        assert ov.skill_allowlist is None

    def test_card_mcp_whitelist(self):
        cfg = self._config()
        mcp = {"playwright": {"command": "x"}, "exa": {"url": "y"}, "other": {"url": "z"}}
        card = AgentCard(mcp_servers=["playwright", "exa"])
        ov = apply_card(cfg, mcp, card)
        assert set(ov.mcp_servers.keys()) == {"playwright", "exa"}

    def test_card_mcp_whitelist_empty_when_none_match(self):
        cfg = self._config()
        mcp = {"a": {"url": "x"}}
        card = AgentCard(mcp_servers=["nonexistent"])
        ov = apply_card(cfg, mcp, card)
        assert ov.mcp_servers == {}

    def test_card_mcp_no_field_means_all(self):
        cfg = self._config()
        mcp = {"a": {"url": "x"}, "b": {"url": "y"}}
        card = AgentCard(mcp_servers=None)
        ov = apply_card(cfg, mcp, card)
        assert ov.mcp_servers == mcp

    def test_card_tools_whitelist(self):
        cfg = self._config()
        card = AgentCard(tools=["Read", "Grep", "mcp__x__y"])
        ov = apply_card(cfg, {}, card)
        assert ov.tool_allowlist == {"Read", "Grep", "mcp__x__y"}

    def test_card_skills_whitelist(self):
        cfg = self._config()
        card = AgentCard(skills=["review", "lint"])
        ov = apply_card(cfg, {}, card)
        assert ov.skill_allowlist == {"review", "lint"}

    def test_card_system_prompt_replaces_built_in(self):
        cfg = self._config()
        card = AgentCard(body="You are a custom agent.")
        ov = apply_card(cfg, {}, card)
        assert ov.system_prompt == "You are a custom agent."

    def test_card_empty_body_falls_back(self):
        cfg = self._config()
        card = AgentCard(body="")
        ov = apply_card(cfg, {}, card)
        assert ov.system_prompt is None

    def test_card_rules_merge_with_config_rules(self):
        cfg = self._config(rules={
            "allowRead": ["./**"],
            "denyWrite": ["config-deny/**"],
            "allowCmd": ["ls"],
        })
        card = AgentCard(rules={
            "denyWrite": ["card-deny/**"],
            "allowCmd": ["cat"],
        })
        ov = apply_card(cfg, {}, card)
        # denyWrite lists concatenate: config first, card appended
        assert ov.rules_raw["denyWrite"] == ["config-deny/**", "card-deny/**"]
        assert ov.rules_raw["allowRead"] == ["./**"]
        assert ov.rules_raw["allowCmd"] == ["ls", "cat"]

    def test_card_rules_when_config_has_none(self):
        cfg = self._config(rules=None)
        card = AgentCard(rules={"denyWrite": ["x/**"]})
        ov = apply_card(cfg, {}, card)
        assert ov.rules_raw == {"denyWrite": ["x/**"]}

    def test_config_rules_when_card_has_none(self):
        cfg = self._config(rules={"denyWrite": ["x/**"]})
        card = AgentCard(rules=None, body="b")
        ov = apply_card(cfg, {}, card)
        assert ov.rules_raw == {"denyWrite": ["x/**"]}


class TestMergeRulesRaw:
    """_merge_rules_raw concatenates per-key lists (config first, card after)."""

    def test_both_none(self):
        assert _merge_rules_raw(None, None) is None

    def test_config_only(self):
        assert _merge_rules_raw({"allowRead": ["a"]}, None) == {"allowRead": ["a"]}

    def test_card_only(self):
        assert _merge_rules_raw(None, {"denyWrite": ["b"]}) == {"denyWrite": ["b"]}

    def test_concatenates_same_key(self):
        merged = _merge_rules_raw(
            {"denyWrite": ["a/**"], "allowRead": ["x"]},
            {"denyWrite": ["b/**"]},
        )
        assert merged["denyWrite"] == ["a/**", "b/**"]
        assert merged["allowRead"] == ["x"]

    def test_non_standard_keys_preserved(self):
        merged = _merge_rules_raw(
            {"customKey": ["x"]},
            {"otherKey": ["y"]},
        )
        assert merged["customKey"] == ["x"]
        assert merged["otherKey"] == ["y"]


# ---------------------------------------------------------------------------
# ToolRunner whitelist enforcement
# ---------------------------------------------------------------------------

class TestToolAllowlistInRunner:
    """ToolRunner filters MCP definitions and blocks non-whitelisted skills."""

    def _runner(self, tool_allowlist=None, skill_allowlist=None, mcp_defs=None):
        class _FakeMcp:
            def get_tool_definitions(self):
                return mcp_defs or []
            async def call_tool(self, name, args):
                return ""
        from letscode.tools import TOOL_DEFINITIONS, EXECUTORS
        return ToolRunner(
            definitions=TOOL_DEFINITIONS,
            executors=EXECUTORS,
            mcp=_FakeMcp(),
            tool_allowlist=tool_allowlist,
            skill_allowlist=skill_allowlist,
        )

    def test_definitions_unfiltered_when_no_allowlist(self):
        runner = self._runner(mcp_defs=[
            {"type": "function", "function": {"name": "mcp__x__y"}},
        ])
        names = [d["function"]["name"] for d in runner.definitions]
        # All built-in tools + the MCP tool
        assert "Bash" in names
        assert "Skill" in names
        assert "mcp__x__y" in names

    def test_definitions_filtered_by_allowlist(self):
        runner = self._runner(
            tool_allowlist={"Read", "mcp__x__y"},
            mcp_defs=[
                {"type": "function", "function": {"name": "mcp__x__y"}},
                {"type": "function", "function": {"name": "mcp__other__z"}},
            ],
        )
        names = [d["function"]["name"] for d in runner.definitions]
        assert set(names) == {"Read", "mcp__x__y"}
        # Built-ins not in allowlist are dropped
        assert "Bash" not in names
        # MCP tools not in allowlist are dropped
        assert "mcp__other__z" not in names

    def test_skill_blocked_when_not_in_allowlist(self):
        runner = self._runner(skill_allowlist={"allowed-skill"})
        results = []
        async def go():
            async for ev in runner.execute('Skill', '{"skill": "blocked"}'):
                results.append(ev)
        asyncio.run(go())
        assert len(results) == 1
        assert "not allowed by agent card" in results[0].content
        assert not results[0].success

    def test_skill_allowed_passes_through(self):
        # "nonexistent_xyz" is not a real skill, but the allowlist check should
        # pass (the skill loader will then report "unknown skill" — which proves
        # the allowlist didn't short-circuit)
        runner = self._runner(skill_allowlist={"nonexistent_xyz"})
        results = []
        async def go():
            async for ev in runner.execute('Skill', '{"skill": "nonexistent_xyz"}'):
                results.append(ev)
        asyncio.run(go())
        assert len(results) == 1
        # Got past the allowlist; failed at skill resolution instead
        assert "not allowed" not in results[0].content

    def test_skill_allowlist_case_insensitive(self):
        runner = self._runner(skill_allowlist={"Review"})
        results = []
        async def go():
            async for ev in runner.execute('Skill', '{"skill": "review"}'):
                results.append(ev)
        asyncio.run(go())
        assert "not allowed" not in results[0].content
