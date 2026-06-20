"""Tests for the Skill tool's discovery and listing."""

from letscode.tools.skill import get_skill_list


class TestGetSkillList:
    """get_skill_list returns {name, description, path} triples. name +
    description feed the system-prompt listing; path is the cached locator
    used by execute() to resolve SKILL.md."""

    def test_returns_path_triple(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "commit"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\nname: commit\ndescription: Create a git commit\n---\n\nbody"
        )

        result = get_skill_list(str(tmp_path))
        assert len(result) == 1
        entry = result[0]
        assert entry["name"] == "commit"
        assert entry["description"] == "Create a git commit"
        assert entry["path"] == str(skill_file)

    def test_empty_when_no_skills(self, tmp_path):
        assert get_skill_list(str(tmp_path)) == []

    def test_missing_description_defaults_to_empty(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "bare"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# no frontmatter here")

        result = get_skill_list(str(tmp_path))
        assert len(result) == 1
        assert result[0]["name"] == "bare"
        assert result[0]["description"] == ""
        assert result[0]["path"] == str(skill_file)

    def test_sorted_by_name(self, tmp_path):
        for name in ("zebra", "alpha", "mango"):
            d = tmp_path / ".claude" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n\nb")

        result = get_skill_list(str(tmp_path))
        assert [r["name"] for r in result] == ["alpha", "mango", "zebra"]
