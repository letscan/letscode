"""Template variable rendering for AgentCard system prompts.

An AgentCard's Markdown body may reference three predefined variables:

- ``{{ env }}`` — the dynamic environment section (CWD/git/platform/shell/OS/model)
- ``{{ skills }}`` — the available-skills listing (filtered by the card's whitelist)
- ``{{ default_system_prompt }}`` — the full built-in default prompt (escape hatch
  for cards that want to keep most default behavior and only prepend/append a few
  lines)

Unknown ``{{ names }}`` are left untouched so legitimate ``{{`` in prose (e.g. code
samples) is not mangled. Only card bodies are rendered — the no-card path calls
``build_system_prompt`` directly with no template layer.
"""

import re

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_card_template(
    body: str,
    *,
    model_id: str,
    skill_allowlist: set[str] | None = None,
) -> str:
    """Render ``{{ env }}`` / ``{{ skills }}`` / ``{{ default_system_prompt }}``.

    Unknown variable names are preserved verbatim. ``skill_allowlist`` (when the
    card restricts skills) is forwarded so ``{{ skills }}`` lists only the
    skills the card permits.
    """
    from .prompt import _env_section, _skills_section, build_system_prompt

    variables = {
        "env": _env_section(model_id),
        "skills": _skills_section(skill_allowlist=skill_allowlist),
        "default_system_prompt": build_system_prompt(model_id),
    }

    def _sub(m: re.Match) -> str:
        return variables.get(m.group(1), m.group(0))

    return _VAR_RE.sub(_sub, body).strip()
