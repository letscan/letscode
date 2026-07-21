"""Passive permission escalation: probe + generalize + session allow-config.

Driven entirely by the ACP server AFTER the best-effort agent loop exits with
a ``permission_denied`` error. The agent itself is unaware escalation is
possible (no RequestPermission tool is exposed in the main loop, no prompt).
See ``docs/plan-permission-escalation.md`` for the design rationale.

This module is server-side only — it runs in the letscode-acp process and
calls the LLM directly via :func:`call_llm` (no subprocess, no feed pollution).

Public surface:

- :data:`REQUEST_PERMISSION_SCHEMA` — the probe's tool schema.
- :func:`probe_permission_request` — two-stage LLM call: (1) decide whether
  to give the agent a retry at all (suppress only if it completed the task),
  (2) extract the minimal set of distinct permissions needed for one retry.
- :func:`normalize_permissions` — deterministic post-probe pass: cmd denials
  with extractable paths become path-level write permissions (the sandbox
  blocks at the syscall level, not the command string); paths resolved to
  absolute form so the allow-rule survives the agent's path-form variation.
- :func:`generalize_target` — deterministic ``allow-always`` pattern derivation.
- :func:`load_session_allow_config` / :func:`add_session_allow_pattern` —
  read/append the per-session ``.letscode/config.<session_id>.json`` file
  that persists allow-always grants.
"""

from __future__ import annotations

import json
import logging
import os
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
            "Declare the minimal set of precise permissions the agent needs "
            "to complete the user's task. Call this when the popup should "
            "fire (the agent did NOT complete the task). If the agent "
            "completed the task despite intermediate denials, do NOT call "
            "this tool — the popup would be noise."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the agent is blocked / what it needs.",
                },
                "permissions": {
                    "type": "array",
                    "description": (
                        "The minimal set of distinct permissions that would "
                        "unblock the task. Deduplicate: when the agent tried "
                        "many command variants (rm, mv, chmod, python, perl, "
                        "sudo, osascript...) against the SAME target, keep "
                        "only the single most direct one. Drop dangerous or "
                        "redundant variants (sudo, osascript) in favor of the "
                        "plain equivalent. One entry per distinct target."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"enum": ["write", "read", "cmd"]},
                            "target": {
                                "type": "string",
                                "description": (
                                    "The exact denied path or command, "
                                    "copied verbatim from the denial list "
                                    "(e.g. '/etc/hosts', 'npm run dev')."
                                ),
                            },
                        },
                        "required": ["type", "target"],
                    },
                },
            },
            "required": ["reason", "permissions"],
        },
    },
}


_PROBE_SYSTEM_PROMPT = (
    "You are inspecting an agent transcript. Work through TWO stages in order.\n\n"
    "STAGE 1 — DECIDE whether to give the agent a retry with relaxed "
    "permissions. The harness trigger (2+ denials, or last tool call denied) "
    "has already fired; a retry popup WILL be shown unless you suppress it. "
    "Suppress ONLY IF the agent ultimately COMPLETED the user's actual "
    "request despite the intermediate denials — in that case the retry "
    "would be pointless noise. The single question for this stage: did the "
    "agent finish what the user asked for?\n"
    "These are NOT completion (do NOT suppress — proceed to STAGE 2):\n"
    "  - Agent tried many approaches (rm, mv, chmod, python, perl, "
    "osascript, swift, sudo...) and ALL were denied — stuck, not done.\n"
    "  - Agent gave up and told the user to run it manually "
    "('你需要手动执行', 'you can run this yourself') — conceding failure.\n"
    "  - Agent partially completed, wrote a fallback, or did something "
    "different from what was asked.\n"
    "  - Agent reported 'sandbox' / 'quarantine' / 'restricted environment'.\n"
    "  - Unsure → do NOT suppress; let the popup through, the user can "
    "reject it.\n\n"
    "STAGE 2 — If STAGE 1 did NOT suppress: you are granting the agent ONE "
    "retry. It can only succeed if the user approves the right permissions, "
    "so extract the MINIMAL set of distinct permissions that would actually "
    "unblock the task — no more, no fewer. Call RequestPermission with that "
    "minimal set. The denial list typically contains many command variants "
    "the agent threw at the same goal; the user should not have to approve "
    "them all. Rules:\n"
    "  - ONE entry per distinct denied target. If the agent tried rm, mv, "
    "chmod, python, perl, swift, sudo, osascript... all against the same "
    "file, that is ONE target — keep the single most direct command "
    "(usually the plainest: prefer `rm <path>` over sudo/osascript/perl "
    "wrappers; prefer a bare write path over a command that writes).\n"
    "  - Drop dangerous or privileged variants when a plain equivalent "
    "exists: `sudo rm ...` → just `rm ...`; `osascript ... delete ...` → "
    "the underlying rm/mv.\n"
    "  - Drop pure diagnostic attempts that aren't the actual operation "
    "(e.g. `xattr -d`, `chmod` used only to enable a later rm, `ls -la` "
    "checks). Keep only what actually performs the denied action.\n"
    "  - Distinct targets (e.g. two different files the user asked to "
    "delete) each get their own entry.\n"
    "  - Copy each target VERBATIM from the denial list (no generalizing "
    "or paraphrasing). The harness derives the allow-rule from this exact "
    "string.\n"
    "The reason field should briefly state what the agent was trying to do."
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
# Path-target extraction (cmd denial → path-level permission)
# ---------------------------------------------------------------------------

# Commands that operate on file paths as positional arguments. When the agent's
# denied command is one of these, the right permission unit is the PATH (a
# write-rule), not the command string — because the sandbox blocks the
# underlying file-write syscall regardless of which command triggered it, and
# a path-level allow survives the agent retrying with a different command
# (rm vs rm -f vs mv). For commands NOT here (npm/git/curl...), the command
# string is the right unit and we don't extract paths.
_PATH_OPERATING_COMMANDS = frozenset({
    "rm", "rmdir", "mv", "cp", "touch", "mkdir", "ln",
    "chmod", "chown", "chflags", "xattr",
    "cat", "tee", "dd",
    "echo", "printf",  # often used as `echo > file` redirections
    "/bin/rm", "/bin/mv", "/bin/cp", "/bin/ln",
    "/bin/chmod", "/bin/chown",
})


def _looks_like_path(token: str) -> bool:
    """Heuristic: does this shell token look like a file path?

    True for absolute paths, home-relative paths, relative paths with
    directory separators, and ``.`` (cwd). False for flags (-f, --force),
    option values, command names, and bare words without slashes (too
    ambiguous — could be an argument value).
    """
    if not token or token == "-":
        return False
    if token == ".":
        return True  # cwd
    if token.startswith("-"):
        return False  # flag
    # Home-relative (~/...) or absolute (/...).
    if token.startswith("~") or token.startswith("/"):
        return True
    # Relative path with a separator (../x, ./x, a/b).
    return "/" in token


def extract_path_targets(command: str) -> list[str]:
    """Extract file-path operands from a denied command string.

    Returns the list of distinct path-like tokens (verbatim, in first-seen
    order). Empty when the command doesn't yield recognizable paths — callers
    fall back to the full command string as the permission target in that case.

    Three sources of paths are mined, in priority order:
    1. Redirection targets (``> path``, ``>> path``) — extracted via regex
       from each command segment. These are write destinations regardless of
       the command (``: > file``, ``echo x >> file``, ``cat > file``).
    2. Positional path arguments of path-operating commands (rm, mv, cp,
       touch, chmod...) — identified via shlex tokenization.
    3. ``sudo``/``doas`` prefix is stripped before command identification so
       ``sudo rm ../.git`` is mined the same as ``rm ../.git``.

    Compound commands (``a && b ; c``) are split, and each segment is examined
    independently so diagnostic segments (``ls``, ``echo "msg"`` without
    redirection) don't pollute the result.

    This is deterministic and replaces what was an unstable LLM extraction
    (the probe would sometimes return ``rm a b && echo done`` as a single
    verbatim "target" — matching nothing when the agent retries, since
    allow-cmd matches the exact string).
    """
    import re
    from ..rules import _split_cmd  # reuse the existing splitter

    # Redirection targets: >, >>, < (the last is a read, but we treat all
    # redirection targets as paths worth surfacing — the caller decides type).
    # Capture the path after the operator, skipping spaces. Match both
    # unquoted and quoted forms.
    _REDIR_RE = re.compile(r"(?:>>|>)\s*([^\s|;&<>]+|'[^']*'|\"[^\"]*\")")

    paths: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        # Strip surrounding quotes from quoted redirection targets.
        if len(p) >= 2 and p[0] in "\"'" and p[-1] == p[0]:
            p = p[1:-1]
        if p and p not in seen and _looks_like_path(p):
            seen.add(p)
            paths.append(p)

    for segment in _split_cmd(command):
        # 1. Redirection targets (work regardless of the command name).
        for m in _REDIR_RE.finditer(segment):
            _add(m.group(1))

        # 2. Positional path args of path-operating commands.
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        # Strip sudo/doas prefix so `sudo rm x` mines like `rm x`.
        while tokens and tokens[0] in ("sudo", "doas"):
            tokens = tokens[1:]
        if not tokens:
            continue
        head = tokens[0]
        if head not in _PATH_OPERATING_COMMANDS:
            continue
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            _add(tok)
    return paths


def _resolve_to_absolute(path: str, cwd: str) -> str:
    """Resolve a path to absolute form, anchored at ``cwd``.

    Relative paths (``../.git``, ``./x``) and ``~/`` paths are expanded.
    Already-absolute paths pass through (after realpath normalization).
    Symlinks are resolved so ``/tmp`` and ``/private/tmp`` (macOS) collapse
    to the same canonical form — matching how the rules engine's
    ``_resolve_pattern`` compares paths.

    This collapses the LLM's path-form instability: whether it picks
    ``../.git`` or ``/Users/.../.git`` from the same denial, the normalized
    permission is the same absolute path, so ``--allow write:/abs/path``
    matches regardless of which form the agent uses on retry.
    """
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


def normalize_permissions(
    perms: list[dict], *, cwd: str | None = None,
) -> list[dict]:
    """Normalize probe-extracted permissions to path-level granularity.

    For each ``cmd`` permission whose command operates on file paths, rewrite
    it to one ``write`` permission PER extracted path — because the sandbox
    blocks at the file-write syscall level (path), not the command string,
    and a path-level allow survives the agent retrying with any command (rm
    vs rm -f vs mv). When path extraction yields nothing (the command isn't
    path-operating, e.g. ``npm run dev``), keep the original ``cmd``
    permission verbatim as the fallback unit.

    Path targets are resolved to **absolute form** (anchored at ``cwd`` when
    given) so the allow-rule matches regardless of whether the agent uses
    relative or absolute paths on retry. This is essential: ``--allow
    write:../.git`` only matches the literal string ``../.git``, but the
    resolved ``--allow write:/abs/path/.git`` matches any form the rules
    engine resolves to the same path.

    ``read`` / ``write`` permissions are also resolved to absolute when
    ``cwd`` is provided. Output is deduplicated by ``(type, target)``
    preserving first-seen order.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for perm in perms:
        typ = perm.get("type")
        target = perm.get("target", "")
        if typ == "cmd":
            paths = extract_path_targets(target)
            if paths:
                # One write permission per path (sandbox gates file-write*).
                for p in paths:
                    rp = _resolve_to_absolute(p, cwd) if cwd else p
                    key = ("write", rp)
                    if key not in seen:
                        seen.add(key)
                        out.append({"type": "write", "target": rp})
                continue
            # Fallback: keep the command string as the unit.
        # Resolve path-type permissions to absolute too.
        if cwd and typ in ("write", "read"):
            target = _resolve_to_absolute(target, cwd)
        key = (typ, target)
        if typ in ("read", "write", "cmd") and target and key not in seen:
            seen.add(key)
            out.append({"type": typ, "target": target})
    return out


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
    cwd: str | None = None,
) -> dict | None:
    """Two-stage decision over an already-triggered permission popup.

    STAGE 1 — decide whether to give the agent a retry with relaxed
    permissions at all. The harness trigger (≥2 denials, or last tool call
    denied) has already fired; the retry popup WILL be shown unless this
    probe suppresses it. Suppression happens ONLY when the agent ultimately
    completed the user's actual request despite the intermediate denials
    (they were dead ends it recovered from). This is NOT a re-decision of
    "was this really a permission block" — second-guessing the trigger is
    what caused the LetsBot delete-scenario misfire (agent tried 13 methods,
    all denied, gave up → probe wrongly read "gave up" as "non-permission
    issue" instead of "task unfinished").

    STAGE 2 — if STAGE 1 did not suppress, the agent gets ONE retry. This
    function extracts the MINIMAL set of distinct permissions that would
    actually unblock the task (collapsing the many command variants the
    agent threw at each target down to the single most direct one).

    Returns ``{"permissions": [{"type", "target"}, ...], "reason"}`` (the
    minimal set, deduplicated) when the popup should fire (STAGE 1 passed,
    STAGE 2 produced a set), or ``None`` when the probe suppresses it
    (STAGE 1 found the task completed). Single-shot via :func:`call_llm`
    — no feed pollution, no subprocess.

    ``transcript`` is a readable text rendering of the session events from
    :func:`feed_util.extract_conversation_text`; ``denials`` is the structured
    list emitted in the ``permission_denied`` error event.
    """
    denials_block = "\n".join(
        f"- type={d.get('type')}, target={d.get('target', '')}"
        for d in denials
    ) or "(none recorded)"

    user_prompt = (
        "Denials that fired the harness trigger:\n"
        f"{denials_block}\n\n"
        "User's original request and what the agent did (transcript, "
        "truncated):\n"
        f"{transcript}\n\n"
        "Work through the two stages:\n"
        "STAGE 1 — did the agent ultimately COMPLETE the user's request "
        "despite these denials? If yes, do NOT call the tool (no retry "
        "needed).\n"
        "STAGE 2 — if no: the agent gets ONE retry. What is the MINIMAL "
        "set of permissions it needs to succeed? Call RequestPermission "
        "with that set."
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
        raw_perms = args.get("permissions")
        # Backward-compat: older probes returned a single "permission" object.
        # Accept it as a one-element list.
        if raw_perms is None and isinstance(args.get("permission"), dict):
            raw_perms = [args["permission"]]
        if not isinstance(raw_perms, list):
            continue
        parsed: list[dict] = []
        for perm in raw_perms:
            if not isinstance(perm, dict):
                continue
            typ = perm.get("type")
            tgt = perm.get("target")
            if typ in ("read", "write", "cmd") and isinstance(tgt, str) and tgt.strip():
                parsed.append({"type": typ, "target": tgt.strip()})
        if not parsed:
            continue
        # Normalize to path-level granularity: a cmd denial like
        # `rm ../a ../b && echo done` becomes two write permissions
        # (write:/abs/a, write:/abs/b), because the sandbox blocks at the
        # file-write syscall (path), not the command string. Paths are
        # resolved to absolute form (anchored at cwd) so the allow-rule
        # matches regardless of the agent's path form on retry. This is the
        # deterministic replacement for an unstable LLM extraction.
        normalized = normalize_permissions(parsed, cwd=cwd)
        if not normalized:
            continue
        return {
            "permissions": normalized,
            "reason": str(args.get("reason", "")).strip(),
        }
    return None


def default_permission_options():
    """Re-export the SDK's standard approve/reject option triple.

    Thin wrapper so server.py imports everything permission-related from a
    single module. The triple is: approve (allow_once), approve_for_session
    (allow_always), reject (reject_once).
    """
    from acp.contrib.permissions import default_permission_options as _impl
    return _impl()


__all__ = [
    "REQUEST_PERMISSION_SCHEMA",
    "default_permission_options",
    "extract_path_targets",
    "generalize_target",
    "load_session_allow_config",
    "add_session_allow_pattern",
    "normalize_permissions",
    "probe_permission_request",
]
