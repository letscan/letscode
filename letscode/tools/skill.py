"""Skill tool — invoke named skill prompts from .claude/skills/ directories."""

import os
import re
from pathlib import Path
from typing import Any

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Skill",
        "description": (
            "Execute a skill within the main conversation\n\n"
            "When users reference a \"slash command\" or \"/<something>\" (e.g., \"/commit\", "
            "\"/review-pr\"), they are referring to a skill. Use this tool to invoke it.\n\n"
            "How to invoke:\n"
            '- Use this tool with the skill name and optional arguments\n'
            "Examples:\n"
            '  - skill: "commit"\n'
            '  - skill: "commit", args: "-m \'Fix bug\'"\n\n'
            "Important:\n"
            "- Available skills are listed in the tool description\n"
            "- When a skill matches the user's request, invoke the relevant Skill tool "
            "BEFORE generating any other response\n"
            "- NEVER mention a skill without actually calling this tool\n"
            "- Do not use this tool for built-in CLI commands (like /help, /clear, etc.)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name to invoke (without the leading /)",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments to pass to the skill",
                },
            },
            "required": ["skill"],
        },
    },
}


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Returns (frontmatter_dict, body_content).
    """
    if not text.startswith("---"):
        return {}, text

    # Find the closing ---
    end = text.find("---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 3:].strip()

    # Minimal YAML-like parsing (key: value pairs)
    frontmatter: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Convert boolean/number strings
            if val.lower() in ("true", "yes"):
                val = True
            elif val.lower() in ("false", "no"):
                val = False
            elif val.isdigit():
                val = int(val)
            frontmatter[key] = val

    return frontmatter, body


def _skill_dirs(cwd: str | None = None) -> list[Path]:
    """Return skill directories to scan, in priority order."""
    dirs: list[Path] = []
    base = cwd or os.getcwd()

    # Project-level: .claude/skills/
    project_skill_dir = Path(base) / ".claude" / "skills"
    if project_skill_dir.is_dir():
        dirs.append(project_skill_dir)

    # Walk up to find parent .claude/skills/
    for parent in Path(base).parents:
        d = parent / ".claude" / "skills"
        if d.is_dir():
            dirs.append(d)
        if (parent / ".git").is_dir():
            break

    # User-level: ~/.claude/skills/
    home_skill_dir = Path.home() / ".claude" / "skills"
    if home_skill_dir.is_dir():
        dirs.append(home_skill_dir)

    return dirs


def _discover_skills(cwd: str | None = None) -> dict[str, Path]:
    """Scan skill directories and return {name: SKILL.md path}."""
    skills: dict[str, Path] = {}

    for skill_dir in _skill_dirs(cwd):
        if not skill_dir.is_dir():
            continue
        for entry in sorted(skill_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if skill_file.exists() and entry.name not in skills:
                skills[entry.name] = skill_file

    return skills


def _expand_template(template: str, args: str | None) -> str:
    """Expand $ARGUMENTS and ${ARGUMENTS} in the template."""
    if args is None:
        # Remove $ARGUMENTS references
        template = template.replace("$ARGUMENTS", "")
        template = re.sub(r"\$\{ARGUMENTS\}", "", template)
        return template

    template = template.replace("$ARGUMENTS", args)
    template = re.sub(r"\$\{ARGUMENTS\}", args, template)
    return template


def execute(args: dict[str, Any]) -> str:
    skill_name = args.get("skill", "").lstrip("/")
    skill_args = args.get("args")

    if not skill_name:
        return "<error>No skill name provided</error>"

    # Discover available skills
    skills = _discover_skills()

    if skill_name not in skills:
        available = ", ".join(sorted(skills.keys())) if skills else "none"
        return f"<error>Unknown skill: '{skill_name}'. Available skills: {available}</error>"

    # Read and parse the skill file
    skill_path = skills[skill_name]
    try:
        raw = skill_path.read_text()
    except Exception as e:
        return f"<error>Failed to read skill '{skill_name}': {e}</error>"

    frontmatter, body = _parse_frontmatter(raw)

    # Expand template variables
    expanded = _expand_template(body, skill_args)

    # Build the skill context header
    lines = [f"[Skill: {skill_name}]"]
    if frontmatter.get("description"):
        lines.append(f"Description: {frontmatter['description']}")
    if skill_args:
        lines.append(f"Arguments: {skill_args}")
    lines.append("")
    lines.append(expanded)

    return "\n".join(lines)


def get_skill_list(cwd: str | None = None) -> list[dict[str, str]]:
    """Return list of {name, description} for available skills."""
    skills = _discover_skills(cwd)
    result = []
    for name, path in sorted(skills.items()):
        try:
            raw = path.read_text()
            fm, _ = _parse_frontmatter(raw)
            result.append({
                "name": name,
                "description": fm.get("description", ""),
            })
        except Exception:
            result.append({"name": name, "description": ""})
    return result
