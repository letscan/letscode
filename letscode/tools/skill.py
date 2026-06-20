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
            "- Available skills are listed in the system prompt under \"Available skills\"\n"
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
    Handles quoted values, values containing colons, and multi-line values.
    """
    if not text.startswith("---"):
        return {}, text

    # Find the closing ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()

    frontmatter: dict[str, Any] = {}
    lines = fm_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue

        # Match key: value — split on the first ": " to allow colons in values
        m = re.match(r'^(\w[\w_-]*)\s*:\s*(.*)', line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()

        # Quoted string (single or double)
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        # Multi-line value (| or > block)
        elif val in ("|", ">") and i < len(lines):
            block_lines: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line[0].isspace():
                    break
                block_lines.append(next_line.strip())
                i += 1
            val = "\n".join(block_lines)
        else:
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
    """Return skill directories to scan, in priority order.

    Searches both .claude/skills/ (client-specific) and .agents/skills/
    (cross-client interop) at each level. .claude/ takes precedence.
    """
    dirs: list[Path] = []
    base = cwd or os.getcwd()

    def _add_skill_bases(root: Path) -> None:
        for client_dir in (".claude", ".agents"):
            d = root / client_dir / "skills"
            if d.is_dir():
                dirs.append(d)

    # Project-level
    _add_skill_bases(Path(base))

    # Walk up to git root
    for parent in Path(base).parents:
        _add_skill_bases(parent)
        if (parent / ".git").is_dir():
            break

    # User-level
    _add_skill_bases(Path.home())

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

    # Case-insensitive lookup: directory name is the canonical key
    resolved = None
    lower = skill_name.lower()
    for name, path in skills.items():
        if name.lower() == lower:
            resolved = (name, path)
            break

    if resolved is None:
        available = ", ".join(sorted(skills.keys())) if skills else "none"
        return f"<error>Unknown skill: '{skill_name}'. Available skills: {available}</error>"

    # Read and parse the skill file
    skill_path = resolved[1]
    canonical_name = resolved[0]
    try:
        raw = skill_path.read_text()
    except Exception as e:
        return f"<error>Failed to read skill '{skill_name}': {e}</error>"

    frontmatter, body = _parse_frontmatter(raw)

    # Expand template variables
    expanded = _expand_template(body, skill_args)

    # Build the skill context header
    lines = [f"[Skill: {canonical_name}]"]
    if frontmatter.get("description"):
        lines.append(f"Description: {frontmatter['description']}")
    if skill_args:
        lines.append(f"Arguments: {skill_args}")
    lines.append("")
    lines.append(expanded)
    content = "\n".join(lines)

    # The expanded skill content is injected into the conversation via a
    # user_message event (consumed by MessageSubscriber), NOT via the tool
    # result string. This frees the return value to carry a concise display
    # label for the CLI/ACP presentation layer.
    from ..events import get_hub
    hub = get_hub()
    if hub:
        hub.emit_user_message_chunk(content)

    return f"Loaded skill {canonical_name} from {skill_path}"


def get_skill_list(cwd: str | None = None) -> list[dict[str, str]]:
    """Return list of {name, description, path} for available skills.

    `name` + `description` feed the system-prompt listing (model discovery);
    `path` is the cached locator the Skill tool uses at execution time, so
    it need not re-scan the filesystem.
    """
    skills = _discover_skills(cwd)
    result = []
    for name, path in sorted(skills.items()):
        try:
            raw = path.read_text()
            fm, _ = _parse_frontmatter(raw)
            result.append({
                "name": name,
                "description": fm.get("description", ""),
                "path": str(path),
            })
        except Exception:
            result.append({"name": name, "description": "", "path": str(path)})
    return result
