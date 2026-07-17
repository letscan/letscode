"""Passive permission escalation: probe + generalize + session allow-config.

Driven entirely by the ACP server AFTER the best-effort agent loop exits with
a ``permission_denied`` error. The agent itself is unaware escalation is
possible (no RequestPermission tool is exposed in the main loop, no prompt).
See ``docs/plan-permission-escalation.md`` for the design rationale.

This module is server-side only — it runs in the letscode-acp process and
calls the LLM directly via :func:`call_llm` (no subprocess, no feed pollution).

Public surface:

- :data:`REQUEST_PERMISSION_SCHEMA` — the probe's single tool schema.
- :func:`generalize_target` — deterministic ``allow-always`` pattern derivation.
- :func:`probe_permission_request` — one-shot LLM call that decides whether
  the run was actually blocked by permissions and extracts the precise target.
- :func:`load_session_allow_config` / :func:`add_session_allow_pattern` —
  read/append the per-session ``.letscode/config.<session_id>.json`` file
  that persists allow-always grants.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any

from ..llm import call_llm

logger = logging.getLogger("letscode-acp")


# ---------------------------------------------------------------------------
# Probe tool schema
# ---------------------------------------------------------------------------

REQUEST_PERMISSION_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "RequestPermission",
        "description": (
            "Declare the precise permission the agent needs to make progress. "
            "Call this ONLY if the task is genuinely blocked by a permission "
            "denial that appeared in the transcript. If the task is already "
            "complete or the blocker is not a permission issue, do NOT call "
            "this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the agent is blocked / what it needs.",
                },
                "permission": {
                    "type": "object",
                    "description": "The precise permission being requested.",
                    "properties": {
                        "type": {"enum": ["write", "read", "cmd"]},
                        "target": {
                            "type": "string",
                            "description": (
                                "The exact denied path or command "
                                "(e.g. '/etc/hosts', 'npm run dev')."
                            ),
                        },
                    },
                    "required": ["type", "target"],
                },
            },
            "required": ["reason", "permission"],
        },
    },
}


_PROBE_SYSTEM_PROMPT = (
    "You are inspecting an agent transcript to decide whether the agent's "
    "task is blocked by a permission denial.\n\n"
    "Rules:\n"
    "1. If the agent was blocked by a permission denial (a tool call "
    "returning an '<error>...denied...</error>' result) and did NOT "
    "complete its task, call the RequestPermission tool with the PRECISE "
    "denied path or command (copy it verbatim from the transcript — do NOT "
    "generalize or paraphrase).\n"
    "2. If the task was completed, or the blocker is NOT a permission "
    "issue (e.g. a real error, missing dependency, wrong approach), do NOT "
    "call the tool. Just reply with a one-line explanation.\n"
    "3. Prefer the MOST RECENT denial if there are several. If several "
    "different targets were denied, pick the one the agent was trying "
    "hardest to use last."
)


# ---------------------------------------------------------------------------
# Deterministic generalization (allow-always pattern derivation)
# ---------------------------------------------------------------------------

# Commands whose first sub-argument is meaningful enough to generalize on.
# e.g. "npm run dev" → "npm run *" (the "run" subcommand is a real category).
# Anything not here generalizes only to the bare command name
# (e.g. "make build" → "make *").
_SUBCOMMAND_NAMES = frozenset({
    "npm", "npx", "yarn", "pnpm", "git", "cargo", "rustup",
    "pip", "pip3", "poetry", "uv", "python", "python3",
    "go", "docker", "kubectl", "helm",
    "mvn", "gradle", "dotnet",
})

# Patterns considered "too broad" — fall back to the exact target if the
# generalization produces one of these (prevents accidentally granting
# everything).
_TOO_BROAD = frozenset({"/**", "**", "*", "/*", "/"})


def generalize_target(typ: str, target: str) -> str:
    """Derive an ``allow-always`` glob pattern from a precise denied target.

    Deterministic (no LLM). One notch broader than the exact target so the
    same category of subsequent operations is covered without re-prompting:

    - **cmd**: split via :func:`shlex.split`. If the command name is in
      :data:`_SUBCOMMAND_NAMES` and a sub-command token follows, generalize
      to ``<cmd> <sub> *`` (e.g. ``npm run dev`` → ``npm run *``). Otherwise
      generalize to the bare command name (``make build`` → ``make *``).
      Single-token commands (``ls``) are not generalized — the pattern equals
      the target.
    - **write / read**: strip the last path segment and append ``*``
      (``/a/b/c.txt`` → ``/a/b/*``; ``./src/x.py`` → ``./src/*``).

    Results that would be too broad (``/**``, ``*``, root) fall back to the
    exact target — never grant more than asked.
    """
    if typ == "cmd":
        return _generalize_cmd(target)
    if typ in ("write", "read"):
        return _generalize_path(target)
    # Unknown type: don't generalize.
    return target


def _generalize_cmd(target: str) -> str:
    try:
        tokens = shlex.split(target.strip())
    except ValueError:
        # Unbalanced quotes — fall back to naive whitespace split.
        tokens = target.strip().split()
    if not tokens:
        return target
    cmd = tokens[0]
    if len(tokens) == 1:
        # Single-token command: no generalization room.
        return target
    # Command with a known subcommand namespace → generalize to sub level.
    if cmd in _SUBCOMMAND_NAMES:
        sub = tokens[1]
        pattern = f"{cmd} {sub} *"
    else:
        pattern = f"{cmd} *"
    return pattern


def _generalize_path(target: str) -> str:
    t = target.rstrip("/")
    if not t or t in _TOO_BROAD:
        return target
    # Strip the last path segment.
    if "/" in t:
        parent = t.rsplit("/", 1)[0]
        if not parent or parent in _TOO_BROAD:
            # Parent would be root or too broad → keep exact target.
            return target
        pattern = parent.rstrip("/") + "/*"
    else:
        # Bare filename (no slashes): no parent to generalize to.
        return target
    if pattern in _TOO_BROAD:
        return target
    return pattern


# ---------------------------------------------------------------------------
# Session allow-config file
# ---------------------------------------------------------------------------

def _session_config_path(cwd: str, session_id: str) -> Path:
    return Path(cwd) / ".letscode" / f"config.{session_id}.json"


def load_session_allow_config(cwd: str, session_id: str) -> dict[str, list[str]]:
    """Load a session's accumulated allow-always rules.

    Returns ``{}`` when the file is absent or unreadable (a missing grant
    must never break a run).
    """
    path = _session_config_path(cwd, session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k in ("allowRead", "allowWrite", "allowCmd"):
        v = data.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v]
    return out


def add_session_allow_pattern(
    cwd: str, session_id: str, typ: str, pattern: str,
) -> None:
    """Append an allow-always pattern to the session config (idempotent).

    ``typ`` is the denial type (``read``/``write``/``cmd``), mapped to the
    matching camelCase rules key. Creates the file if absent.
    """
    key = _type_to_rules_key(typ)
    if key is None:
        return
    path = _session_config_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    lst = data.get(key) or []
    if not isinstance(lst, list):
        lst = []
    if pattern not in lst:
        lst.append(pattern)
    data[key] = lst
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _type_to_rules_key(typ: str) -> str | None:
    return {"read": "allowRead", "write": "allowWrite", "cmd": "allowCmd"}.get(typ)


# ---------------------------------------------------------------------------
# Probe — single-shot LLM call to extract the precise permission request
# ---------------------------------------------------------------------------

async def probe_permission_request(
    denials: list[dict],
    transcript: str,
    *,
    model_id: str | None = None,
    config_path: str | None = None,
) -> dict | None:
    """Ask the LLM whether the run was permission-blocked and extract the target.

    Returns a ``{"type", "target", "reason"}`` dict when the LLM calls
    ``RequestPermission``, or ``None`` when it doesn't (task complete or
    non-permission blocker). Single-shot via :func:`call_llm` — no feed
    pollution, no subprocess.

    ``transcript`` is a readable text rendering of the session events from
    :func:`feed_util.extract_conversation_text`; ``denials`` is the structured
    list emitted in the ``permission_denied`` error event.
    """
    denials_block = "\n".join(
        f"- type={d.get('type')}, target={d.get('target', '')}"
        for d in denials
    ) or "(none recorded)"

    user_prompt = (
        "Recent agent transcript (truncated):\n\n"
        f"{transcript}\n\n"
        "Recorded permission denials this session:\n"
        f"{denials_block}\n\n"
        "Decide: was the agent's task blocked by a permission denial? "
        "If yes, call RequestPermission with the precise denied target."
    )

    try:
        result = await call_llm(
            [{"type": "text", "text": user_prompt}],
            system_prompt=_PROBE_SYSTEM_PROMPT,
            model_id=model_id,
            config_path=config_path,
            tools=[REQUEST_PERMISSION_SCHEMA],
            purpose="permission-probe",
        )
    except Exception:
        logger.warning("Permission probe call failed", exc_info=True)
        return None

    tool_calls = result.tool_calls or []
    for tc in tool_calls:
        if tc.name != "RequestPermission":
            continue
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            continue
        perm = args.get("permission") or {}
        typ = perm.get("type")
        tgt = perm.get("target")
        if typ in ("read", "write", "cmd") and isinstance(tgt, str) and tgt.strip():
            return {
                "type": typ,
                "target": tgt.strip(),
                "reason": str(args.get("reason", "")).strip(),
            }
    return None


__all__ = [
    "REQUEST_PERMISSION_SCHEMA",
    "default_permission_options",
    "generalize_target",
    "load_session_allow_config",
    "add_session_allow_pattern",
    "probe_permission_request",
]


def default_permission_options():
    """Re-export the SDK's standard approve/reject option triple.

    Thin wrapper so server.py imports everything permission-related from a
    single module. The triple is: approve (allow_once), approve_for_session
    (allow_always), reject (reject_once).
    """
    from acp.contrib.permissions import default_permission_options as _impl
    return _impl()
