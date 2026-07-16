"""Tests for the ``add_scan_dirs`` config option and ``--add-scan-dir`` flag.

Each entry in ``add_scan_dirs`` is an extra root scanned for skills
(``<dir>/skills/<name>/SKILL.md``) and agent cards (``<dir>/agents/<Name>.md``)
at the lowest priority: project/walk-up/user dirs win on name collision
(first-wins). Surfaced in both the CLI layer (config + flag merged) and the
ACP layer (loaded once in __init__, forwarded to discovery + subprocess argv).
"""

import json
import tempfile

import pytest

from letscode.agent_card import discover_agent_cards
from letscode.config import load_config, load_scan_dirs
from letscode.prompt import _skills_section, build_system_prompt
from letscode.tools.skill import get_skill_list, set_scan_dirs


# ── Fixtures ──

def _write_config(tmp_path, **extra) -> str:
    """Write a minimal config.json with the given top-level extras."""
    cfg = {"default_model": "m", "providers": {"p": {"models": [{"model": "m"}]}}, **extra}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def _make_shared_dir(tmp_path, *, skills=(), cards=()) -> str:
    """Build a shared dir with ``skills/<name>/SKILL.md`` and ``agents/<X>.md``.

    ``skills`` / ``cards`` are iterables of (name, description) tuples.
    """
    shared = tmp_path / "shared"
    for name, desc in skills:
        sd = shared / "skills" / name
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(f"---\ndescription: {desc}\n---\nbody")
    for name, desc in cards:
        ad = shared / "agents"
        ad.mkdir(parents=True, exist_ok=True)
        (ad / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody"
        )
    return str(shared)


@pytest.fixture(autouse=True)
def _reset_runtime_scan_dirs():
    """Ensure the module-level runtime scan dirs don't leak between tests."""
    set_scan_dirs([])
    yield
    set_scan_dirs([])


# ── Config layer ──

class TestConfigScanDirs:
    def test_default_empty(self, tmp_path):
        path = _write_config(tmp_path)
        cfg, _ = load_config(path, "m")
        assert cfg.add_scan_dirs == []
        assert load_scan_dirs(path) == []

    def test_reads_add_scan_dirs(self, tmp_path):
        path = _write_config(tmp_path, add_scan_dirs=["/a/b", "/c"])
        cfg, _ = load_config(path, "m")
        assert cfg.add_scan_dirs == ["/a/b", "/c"]
        assert load_scan_dirs(path) == ["/a/b", "/c"]

    def test_non_list_falls_back_to_empty(self, tmp_path):
        # A malformed (non-list) add_scan_dirs is coerced to [] rather than crash.
        path = _write_config(tmp_path, add_scan_dirs="not-a-list")
        assert load_scan_dirs(path) == []

    def test_elements_coerced_to_str(self, tmp_path):
        path = _write_config(tmp_path, add_scan_dirs=[1, 2])
        assert load_scan_dirs(path) == ["1", "2"]


# ── Skill discovery ──

class TestSkillDiscovery:
    def test_scan_dirs_skill_found(self, tmp_path):
        shared = _make_shared_dir(tmp_path, skills=[("foo", "foo skill")])
        skills = get_skill_list(cwd=str(tmp_path / "proj"), scan_dirs=[shared])
        names = {s["name"] for s in skills}
        assert "foo" in names

    def test_no_scan_dirs_no_extra_skill(self, tmp_path):
        _make_shared_dir(tmp_path, skills=[("foo", "foo skill")])
        skills = get_skill_list(cwd=str(tmp_path / "proj"), scan_dirs=None)
        # foo lives only under shared/ — without scan_dirs it's absent.
        # (builtins/user skills may exist, but not "foo")
        names = {s["name"] for s in skills}
        assert "foo" not in names

    def test_project_skill_wins_over_scan_dir(self, tmp_path):
        # A project-level skill with the same name as a scan-dir skill should
        # win (first-wins: project dirs are scanned before scan_dirs).
        shared = _make_shared_dir(tmp_path, skills=[("dupe", "from-shared")])
        proj = tmp_path / "proj"
        (proj / ".claude" / "skills" / "dupe").mkdir(parents=True)
        (proj / ".claude" / "skills" / "dupe" / "SKILL.md").write_text(
            "---\ndescription: from-project\n---\nproject body"
        )
        skills = {s["name"]: s for s in get_skill_list(
            cwd=str(proj), scan_dirs=[shared],
        )}
        assert skills["dupe"]["description"] == "from-project"

    def test_runtime_set_scan_dirs_used_by_execute(self, tmp_path):
        # execute() calls _discover_skills() with no args; the runtime module
        # state set via set_scan_dirs must be picked up.
        shared = _make_shared_dir(tmp_path, skills=[("bar", "bar skill")])
        set_scan_dirs([shared])
        from letscode.tools.skill import _discover_skills
        skills = _discover_skills(cwd=str(tmp_path / "proj"))
        assert "bar" in skills


# ── Card discovery ──

class TestCardDiscovery:
    def test_scan_dirs_card_found(self, tmp_path):
        shared = _make_shared_dir(tmp_path, cards=[("Auditor", "audit card")])
        cards = discover_agent_cards(cwd=str(tmp_path / "proj"), scan_dirs=[shared])
        assert "auditor" in cards

    def test_no_scan_dirs_no_extra_card(self, tmp_path):
        _make_shared_dir(tmp_path, cards=[("Auditor", "audit card")])
        cards = discover_agent_cards(cwd=str(tmp_path / "proj"), scan_dirs=None)
        assert "auditor" not in cards

    def test_project_card_wins_over_scan_dir(self, tmp_path):
        shared = _make_shared_dir(tmp_path, cards=[("Dupe", "from-shared")])
        proj = tmp_path / "proj"
        (proj / "agents").mkdir(parents=True)
        (proj / "agents" / "Dupe.md").write_text(
            "---\nname: Dupe\ndescription: from-project\n---\nproject body"
        )
        cards = discover_agent_cards(cwd=str(proj), scan_dirs=[shared])
        from letscode.agent_card import _parse_card
        card = _parse_card(cards["dupe"].read_text(encoding="utf-8"))
        assert card.description == "from-project"

    def test_scan_dir_does_not_override_builtin(self, tmp_path):
        # A scan-dir card whose stem matches a builtin (e.g. "review") must not
        # shadow the builtin (first-wins: builtins scanned before scan_dirs).
        shared = _make_shared_dir(tmp_path, cards=[("Review", "fake review")])
        cards = discover_agent_cards(cwd=str(tmp_path / "proj"), scan_dirs=[shared])
        assert "review" in cards
        from letscode.agent_card import _parse_card
        card = _parse_card(cards["review"].read_text(encoding="utf-8"))
        # builtin description, not the fake one
        assert card.description != "fake review"


# ── Prompt layer ──

class TestPromptScanDirs:
    def test_skills_section_lists_scan_dir_skill(self, tmp_path):
        shared = _make_shared_dir(tmp_path, skills=[("zed", "zed skill")])
        section = _skills_section(scan_dirs=[shared])
        assert "zed" in section
        assert "zed skill" in section

    def test_build_system_prompt_threads_scan_dirs(self, tmp_path):
        shared = _make_shared_dir(tmp_path, skills=[("zed", "zed skill")])
        prompt = build_system_prompt("model-x", scan_dirs=[shared])
        assert "zed" in prompt


# ── CLI flag merge ──

class TestCliMergeScanDirs:
    def test_merge_config_then_cli_dedup(self):
        from letscode.cli import _merge_scan_dirs
        # config dirs first, CLI appended, de-duplicated, order preserved
        assert _merge_scan_dirs(["/a", "/b"], ["/b", "/c"]) == ["/a", "/b", "/c"]

    def test_merge_empty_config(self):
        from letscode.cli import _merge_scan_dirs
        assert _merge_scan_dirs([], ["/x"]) == ["/x"]

    def test_merge_no_cli(self):
        from letscode.cli import _merge_scan_dirs
        assert _merge_scan_dirs(["/a"], None) == ["/a"]

    def test_merge_all_empty(self):
        from letscode.cli import _merge_scan_dirs
        assert _merge_scan_dirs([], None) == []
