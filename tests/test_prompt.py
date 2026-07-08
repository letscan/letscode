"""Tests for system prompt construction, especially dynamic sections."""

import os

from letscode.prompt import _skills_section, build_system_prompt


class TestSkillsSection:
    """The skills section injects name + description (no path) so the model
    can discover and trigger skills. Empty when no skills are present."""

    def test_empty_when_no_skills(self, monkeypatch, tmp_path):
        # cwd with no .claude/skills or .agents/skills
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        assert _skills_section() == ""

    def test_lists_name_and_description(self, monkeypatch, tmp_path):
        # Build a fake skill tree under a project .claude/skills/
        skill_dir = tmp_path / ".claude" / "skills" / "commit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: commit\ndescription: Create a git commit\n---\n\nbody"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        section = _skills_section()
        assert "# Available skills" in section
        assert "- commit: Create a git commit" in section
        # Path must NOT be injected into the system prompt
        assert "SKILL.md" not in section
        assert str(skill_dir) not in section

    def test_skill_without_description(self, monkeypatch, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "bare"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# just a body, no frontmatter")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        section = _skills_section()
        assert "- bare" in section

    def test_skill_allowlist_filters_listing(self, monkeypatch, tmp_path):
        # When an AgentCard restricts skills, _skills_section must list only
        # the permitted ones so the prompt's catalog matches what the Skill
        # tool will actually allow at execution time.
        for name, desc in [("deploy", "Deploy"), ("commit", "Commit")]:
            d = tmp_path / ".claude" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n\nbody"
            )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        section = _skills_section(skill_allowlist={"commit"})
        assert "- commit: Commit" in section
        assert "deploy" not in section

    def test_skill_allowlist_case_insensitive(self, monkeypatch, tmp_path):
        d = tmp_path / ".claude" / "skills" / "deploy"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Deploy\n---\n\nbody"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        section = _skills_section(skill_allowlist={"DEPLOY"})
        assert "- deploy: Deploy" in section


class TestBuildSystemPrompt:
    """Skills section is appended after env; absence leaves no trace."""

    def test_no_skills_section_when_empty(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        prompt = build_system_prompt("test-model")
        assert "# Available skills" not in prompt

    def test_skills_section_present_when_skills_exist(self, monkeypatch, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Deploy the app\n---\n\nbody"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        prompt = build_system_prompt("test-model")
        assert "# Available skills" in prompt
        assert "- deploy: Deploy the app" in prompt
