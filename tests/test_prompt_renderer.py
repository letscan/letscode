"""Tests for AgentCard template variable rendering ({{ env }} / {{ skills }}
/ {{ default_system_prompt }})."""

from letscode.prompt_renderer import render_card_template


class TestRenderCardTemplate:
    """The renderer substitutes the three known variables and leaves unknown
    ``{{ names }}`` untouched so prose isn't mangled."""

    def test_env_substituted(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        rendered = render_card_template("CWD is {{ env }}", model_id="m1")
        assert "{{ env }}" not in rendered
        assert "# Environment" in rendered
        assert str(tmp_path) in rendered

    def test_skills_substituted_when_present(self, monkeypatch, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: Deploy the app\n---\n\nbody"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        rendered = render_card_template("{{ skills }}", model_id="m")
        assert "# Available skills" in rendered
        assert "- deploy: Deploy the app" in rendered

    def test_skills_empty_string_when_none_available(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        rendered = render_card_template("[{{ skills }}]", model_id="m")
        # No skills → empty section → renders as [] after strip of the inner var
        assert "{{ skills }}" not in rendered

    def test_skills_filtered_by_allowlist(self, monkeypatch, tmp_path):
        # Two skills present; card allows only one
        for name, desc in [("deploy", "Deploy"), ("commit", "Commit")]:
            d = tmp_path / ".claude" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n\nbody"
            )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))

        rendered = render_card_template(
            "{{ skills }}", model_id="m", skill_allowlist={"commit"},
        )
        assert "- commit: Commit" in rendered
        assert "deploy" not in rendered

    def test_default_system_prompt_substituted(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        rendered = render_card_template(
            "Prefix\n\n{{ default_system_prompt }}", model_id="test-model",
        )
        assert "{{ default_system_prompt }}" not in rendered
        assert "Prefix" in rendered
        # The default prompt contains its well-known sections
        assert "# Environment" in rendered
        assert "You are LetsCode" in rendered

    def test_unknown_variable_preserved(self):
        rendered = render_card_template(
            "see {{ unknown_var }} here", model_id="m",
        )
        assert "{{ unknown_var }}" in rendered

    def test_no_spaces_around_name(self, monkeypatch, tmp_path):
        # {{env}} (no inner spaces) should match {{ env }}
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        rendered = render_card_template("{{env}}", model_id="m")
        assert "{{env}}" not in rendered
        assert "# Environment" in rendered

    def test_multiple_variables_in_one_body(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
        body = "Before\n\n{{ env }}\n\n{{ default_system_prompt }}\n\nAfter"
        rendered = render_card_template(body, model_id="m")
        assert "Before" in rendered
        assert "After" in rendered
        assert "{{ " not in rendered

    def test_body_without_variables_unchanged(self):
        body = "Just plain text with no variables."
        rendered = render_card_template(body, model_id="m")
        assert rendered == body

    def test_double_brace_in_prose_preserved(self):
        # Legitimate {{ in code samples must not be touched when it's not a
        # recognized variable name.
        body = "In Jinja: {{ user.name }} and {{ env }}"
        rendered = render_card_template(body, model_id="m")
        # {{ env }} is recognized → substituted; {{ user.name }} is not a bare
        # word (\w+ doesn't match the dot) → preserved
        assert "{{ user.name }}" in rendered
