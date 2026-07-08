"""AgentCard — Markdown + YAML frontmatter definition of an agent persona.

An AgentCard is a ``agents/<Name>.md`` file whose YAML frontmatter declares
capability boundaries (available tools, skills, MCP servers, permission rules)
and whose Markdown body becomes the system prompt when the agent runs.

This module is the **single merge point** between a card and the loaded
``config.json``: :func:`apply_card` takes ``(config, mcp_servers, card)`` and
produces a :class:`CardOverrides` with all card effects resolved. Callers never
branch on ``card is None`` — ``apply_card`` returns all-default overrides when
no card is active.

Card fields (6): name, description, tools, skills, mcp_servers, rules. These
do not overlap with existing CLI override knobs (preset/sandbox/effort/--model),
so priority is clean: the only intersection is ``--no-mcp``, which zeros out
``mcp_servers`` *after* this merge runs, so ``CLI > card`` holds naturally.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentCard:
    """Parsed AgentCard frontmatter + body.

    ``None`` for a whitelist field means "no restriction" (everything allowed).
    """
    name: str | None = None
    description: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None
    rules: dict | None = None
    preset: str | None = None
    body: str = ""


@dataclass
class CardOverrides:
    """Card effects merged onto config. All-default when no card is active.

    - ``mcp_servers``: card whitelist applied (or unchanged when no card)
    - ``rules_raw``: config.rules deep-merged with card.rules (camelCase dict)
    - ``preset``: card's sandbox preset (safe/default/risk), or None → caller
      keeps config.preset. Applied to ModelConfig by the caller so that
      merge_rules(preset, ...) sees the card's intent.
    - ``system_prompt``: card body, or None → caller falls back to built-in prompt
    - ``tool_allowlist`` / ``skill_allowlist``: None means unrestricted
    """
    mcp_servers: dict = field(default_factory=dict)
    rules_raw: dict | None = None
    preset: str | None = None
    system_prompt: str | None = None
    tool_allowlist: set[str] | None = None
    skill_allowlist: set[str] | None = None


def _agents_dir(cwd: str | None = None) -> Path:
    """Return the project-root ``agents/`` directory (relative to cwd)."""
    base = cwd or os.getcwd()
    return Path(base) / "agents"


def _discover_builtin_cards() -> dict:
    """Return ``{stem_lower: path}`` for cards shipped with the package.

    Builtin cards live in ``letscode/builtin_agents/*.md`` and are read via
    ``importlib.resources`` so they work under both editable and wheel installs.
    A project-level ``agents/<Name>.md`` with the same stem overrides the
    builtin (see :func:`discover_agent_cards`).
    """
    from importlib.resources import files

    cards: dict = {}
    d = files("letscode.builtin_agents")
    if not d.is_dir():
        return cards
    for entry in d.iterdir():
        if entry.is_file() and entry.name.endswith(".md"):
            cards[entry.name[:-3].lower()] = entry
    return cards


def discover_agent_cards(cwd: str | None = None) -> dict:
    """Return ``{stem_lower: path}`` for all available cards.

    Builtin cards (shipped with the package) form the base layer; any
    ``agents/*.md`` in the project overrides a builtin with the same stem
    (case-insensitive). Each ``.md`` file directly under ``agents/`` is one
    card; subdirectories are not scanned (single-directory convention).

    Paths may be :class:`pathlib.Path` (project cards) or
    ``importlib.resources.Traversable`` (builtins); both expose
    ``read_text()``/``is_file()``.
    """
    cards: dict = _discover_builtin_cards()  # low priority — overridable
    d = _agents_dir(cwd)
    if d.is_dir():
        for entry in sorted(d.iterdir()):
            if entry.is_file() and entry.suffix == ".md":
                cards[entry.stem.lower()] = entry
    return cards


def load_builtin_card(name: str) -> AgentCard:
    """Load a card bundled with the package (e.g. 'Explore').

    Reads from ``letscode/builtin_agents/<Name>.md`` via importlib.resources.
    Raises :class:`SystemExit` if no such builtin exists.
    """
    from importlib.resources import files

    builtin = files("letscode.builtin_agents") / f"{name}.md"
    if not builtin.is_file():
        raise SystemExit(f"No built-in agent card named {name!r}.")
    return _parse_card(builtin.read_text(encoding="utf-8"))


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a markdown file into ``(frontmatter_text, body)``.

    Frontmatter is delimited by a leading ``---`` line and a closing ``---``
    line (optionally with trailing whitespace), matching the existing skill
    convention. Returns ``("", text)`` when no frontmatter is present.
    ``frontmatter_text`` excludes the delimiters.

    Line-based parsing avoids the regex boundary pitfalls that tripped earlier
    versions (e.g. an empty frontmatter block where the closing ``---`` has no
    preceding newline, or a closing ``---`` followed by body content).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    # Find the closing "---" line (the first one after the opener whose stripped
    # content is exactly "---", possibly with trailing spaces/tabs).
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i]).strip()
            body = "\n".join(lines[i + 1:]).strip()
            return fm_text, body
    # Opening delimiter present but no closing → treat whole text as body
    return "", text


def _parse_card(text: str) -> AgentCard:
    """Parse card text into an :class:`AgentCard` using YAML frontmatter."""
    fm_text, body = _split_frontmatter(text)
    card = AgentCard(body=body)
    if not fm_text:
        return card

    data = yaml.safe_load(fm_text)
    if not isinstance(data, dict):
        # Non-mapping frontmatter (e.g. a bare string) — treat as no config
        return card

    if isinstance(data.get("name"), str):
        card.name = data["name"]
    if isinstance(data.get("description"), str):
        card.description = data["description"]
    if isinstance(data.get("tools"), list):
        card.tools = [str(t) for t in data["tools"]]
    if isinstance(data.get("skills"), list):
        card.skills = [str(s) for s in data["skills"]]
    if isinstance(data.get("mcp_servers"), list):
        card.mcp_servers = [str(s) for s in data["mcp_servers"]]
    if isinstance(data.get("rules"), dict):
        card.rules = data["rules"]
    if isinstance(data.get("preset"), str):
        card.preset = data["preset"]
    return card


def load_agent_card(name: str, cwd: str | None = None) -> AgentCard:
    """Load and parse an AgentCard by name (case-insensitive stem match).

    Raises :class:`SystemExit` listing available cards when not found.
    """
    cards = discover_agent_cards(cwd)
    lower = name.lower()
    path = cards.get(lower)
    if path is None:
        available = ", ".join(sorted(cards.keys())) or "(none)"
        raise SystemExit(
            f"Agent card {name!r} not found. Available: {available}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"Failed to read agent card {path}: {e}")
    return _parse_card(text)


def _merge_rules_raw(
    config_rules: dict | None, card_rules: dict | None,
) -> dict | None:
    """Deep-merge config.rules with card.rules (camelCase keys, list values).

    Each of the 6 rule keys (allowRead, denyRead, allowWrite, denyWrite,
    allowCmd, denyCmd) is concatenated: config values first, card values
    appended. Returns ``None`` when neither side declares any rules.
    """
    if config_rules is None and card_rules is None:
        return None
    if config_rules is None:
        return dict(card_rules or {})
    if card_rules is None:
        return dict(config_rules)
    keys = ("allowRead", "denyRead", "allowWrite",
            "denyWrite", "allowCmd", "denyCmd")
    merged: dict = {}
    for k in keys:
        base = list(config_rules.get(k) or [])
        extra = list(card_rules.get(k) or [])
        if base or extra:
            merged[k] = [*base, *extra]
    # Preserve any non-standard keys from config (forward compat)
    for k, v in config_rules.items():
        if k not in merged and k not in keys:
            merged[k] = v
    for k, v in card_rules.items():
        if k not in merged and k not in keys:
            merged[k] = v
    return merged or None


def apply_card(
    config, mcp_servers: dict, card: AgentCard | None,
) -> CardOverrides:
    """Merge ``card`` onto ``(config, mcp_servers)`` — the single merge point.

    Returns all-default :class:`CardOverrides` when ``card`` is None, so callers
    consume the result uniformly without per-field null checks.
    """
    if card is None:
        return CardOverrides(
            mcp_servers=dict(mcp_servers),
            rules_raw=config.rules,
            preset=None,
            system_prompt=None,
            tool_allowlist=None,
            skill_allowlist=None,
        )

    # mcp_servers whitelist: keep only the servers the card names
    if card.mcp_servers is not None:
        allow = {s for s in card.mcp_servers}
        filtered = {k: v for k, v in mcp_servers.items() if k in allow}
    else:
        filtered = dict(mcp_servers)

    return CardOverrides(
        mcp_servers=filtered,
        rules_raw=_merge_rules_raw(config.rules, card.rules),
        preset=card.preset,
        system_prompt=card.body or None,
        tool_allowlist=set(card.tools) if card.tools is not None else None,
        skill_allowlist=set(card.skills) if card.skills is not None else None,
    )
