"""Fine-grained access rules engine for path and command restrictions."""

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

_SECRET_COMPONENTS = frozenset(
    [".ssh", ".aws", ".gnupg", ".netrc", "credentials"]
)


@dataclass
class Rules:
    allow_read: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    allow_cmd: list[str] = field(default_factory=list)
    deny_cmd: list[str] = field(default_factory=list)


def load_rules(raw: dict | None) -> Rules | None:
    """Parse rules from config.json 'rules' field."""
    if not raw:
        return None
    r = Rules()
    r.allow_read = raw.get("allowRead", [])
    r.deny_read = raw.get("denyRead", [])
    r.allow_write = raw.get("allowWrite", [])
    r.deny_write = raw.get("denyWrite", [])
    r.allow_cmd = raw.get("allowCmd", [])
    r.deny_cmd = raw.get("denyCmd", [])
    return r


# Preset rule definitions — each preset is a predefined set of allow/deny rules.
PRESET_RULES: dict[str, Rules] = {
    "safe": Rules(
        deny_write=["/**"],
    ),
    "default": Rules(
        allow_write=["./**"],
    ),
    "risk": Rules(
        allow_write=["/**"],
    ),
}


def merge_rules(preset: str, user_rules: Rules | None) -> Rules:
    """Merge preset rules with user-configured rules.

    User rules extend preset rules. Deny rules take priority over allow rules
    in the check functions.
    """
    base = PRESET_RULES.get(preset, Rules())
    if user_rules is None:
        return base
    return Rules(
        allow_read=[*base.allow_read, *user_rules.allow_read],
        deny_read=[*base.deny_read, *user_rules.deny_read],
        allow_write=[*base.allow_write, *user_rules.allow_write],
        deny_write=[*base.deny_write, *user_rules.deny_write],
        allow_cmd=[*base.allow_cmd, *user_rules.allow_cmd],
        deny_cmd=[*base.deny_cmd, *user_rules.deny_cmd],
    )


def _is_secret_path(resolved: str) -> bool:
    """Check if a resolved path contains secret components.

    Uses path-component matching instead of substring to avoid
    false positives like 'my_credentials_report.csv'.
    """
    parts = PurePosixPath(resolved).parts
    for part in parts:
        if part in _SECRET_COMPONENTS:
            return True
        # .env, .env.local, .env.production etc.
        if part == ".env" or part.startswith(".env."):
            return True
    return False


def _resolve_pattern(pattern: str, cwd: str) -> tuple[str, bool]:
    """Resolve a rule pattern.

    Returns (resolved_pattern, is_recursive).
    **-prefixed patterns are kept as-is for PurePath.match.
    """
    if pattern.startswith("**/"):
        return pattern, True
    if pattern.startswith("~/"):
        return os.path.realpath(Path.home() / pattern[2:]), False
    if pattern.startswith("./"):
        return os.path.realpath(os.path.join(cwd, pattern[2:])), False
    if pattern.startswith("/"):
        return os.path.realpath(pattern), False
    return os.path.realpath(os.path.join(cwd, pattern)), False


def _glob_match(path: str, pattern: str, is_recursive: bool) -> bool:
    """Match a resolved absolute path against a pattern."""
    p = PurePosixPath(path)

    # **-prefixed patterns: match anywhere in the tree
    if is_recursive:
        return p.match(pattern)

    # ./prefix/** — tree prefix match
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return str(p).startswith(prefix + "/") or str(p) == prefix

    # Standard fnmatch for simple patterns
    return fnmatch(str(p), pattern)


def _match_path(path: str, patterns: list[str], cwd: str) -> bool:
    """Check if path matches any of the glob patterns."""
    if not patterns:
        return False
    resolved = str(Path(path).resolve())
    for pat in patterns:
        target, is_recursive = _resolve_pattern(pat, cwd)
        if _glob_match(resolved, target, is_recursive):
            return True
    return False


def _pattern_specificity(pattern: str) -> tuple[int, int, int]:
    """Compute a specificity tuple ``(anchored, depth, prefix_len)``.

    Used to rank competing allow/deny matches: a more specific pattern wins,
    and ties break to deny (safety invariant). Specificity is computed from
    the **original** pattern (user intent), not from the resolved absolute
    path — otherwise a bare ``plan.md`` would inherit the cwd's depth.

    - ``anchored``: patterns rooted to a location (``/``, ``./``, ``~/``) or a
      bare filename (semantically relative to cwd, like ``./``) rank above pure
      wildcards (``**/x`` matches anywhere in the tree). Only ``**/``-prefixed
      patterns are unanchored.
    - ``depth``: number of complete literal path segments before the first
      wildcard char. ``/a/b/*`` → 2; ``/a/*`` → 1; ``/**`` → 0; ``plan.md`` → 1.
    - ``prefix_len``: character length of the literal prefix (before the first
      ``*``/``?``); breaks ties at equal depth.

    This generalizes nginx's longest-prefix-match: a specific allow overrides
    a broader deny (the documented "escape hatch for broad deny rules").
    """
    if pattern.startswith("**/"):
        return (0, 0, 0)
    p = pattern[2:] if pattern.startswith("./") else pattern
    # Literal prefix = everything before the first glob char
    glob_idx = len(p)
    for ch in ("*", "?"):
        idx = p.find(ch)
        if idx != -1:
            glob_idx = min(glob_idx, idx)
    literal = p[:glob_idx]
    segments = [s for s in literal.strip("/").split("/") if s]
    return (1, len(segments), len(literal))


def _most_specific_match(
    path: str, patterns: list[str], cwd: str,
) -> str | None:
    """Return the most specific pattern in ``patterns`` matching ``path``.

    Returns ``None`` when nothing matches. Ties (equal specificity) keep the
    first-listed pattern — callers compare allow vs deny specificity and apply
    deny-wins-on-tie at that layer.
    """
    if not patterns:
        return None
    resolved = str(Path(path).resolve())
    best: str | None = None
    best_spec: tuple[int, int, int] = (-1, -1, -1)
    for pat in patterns:
        target, is_recursive = _resolve_pattern(pat, cwd)
        if _glob_match(resolved, target, is_recursive):
            spec = _pattern_specificity(pat)
            if spec > best_spec:
                best, best_spec = pat, spec
    return best


def _has_shell_expansion(command: str) -> bool:
    """Detect dangerous shell metacharacters that bypass simple pattern matching.

    Catches command substitution $(...), backticks, and process substitution <(…) >(…).
    These constructs allow executing arbitrary commands invisible to fnmatch-based checks.
    """
    # Command substitution: $(...)
    if "$(" in command:
        return True
    # Backtick command substitution
    if "`" in command:
        return True
    # Process substitution
    if "<(" in command or ">(" in command:
        return True
    return False


def _split_cmd(command: str) -> list[str]:
    """Split a shell command into sub-commands by ; && || |.

    Handles quoted strings and escaped characters to avoid splitting
    inside quoted content.
    """
    parts = []
    current: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]

        # Skip content inside single quotes
        if c == "'":
            current.append(c)
            i += 1
            while i < n and command[i] != "'":
                current.append(command[i])
                i += 1
            if i < n:
                current.append(command[i])
                i += 1
            continue

        # Skip content inside double quotes (respect backslash escapes)
        if c == '"':
            current.append(c)
            i += 1
            while i < n and command[i] != '"':
                if command[i] == '\\' and i + 1 < n:
                    current.append(command[i])
                    i += 1
                current.append(command[i])
                i += 1
            if i < n:
                current.append(command[i])
                i += 1
            continue

        if c in (';', '|', '&'):
            if current:
                parts.append("".join(current).strip())
                current = []
            # Skip && || sequences
            if i + 1 < n and command[i + 1] in ('&', '|'):
                i += 1
        elif c == '\n':
            if current:
                parts.append("".join(current).strip())
                current = []
        else:
            current.append(c)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _match_cmd(command: str, patterns: list[str]) -> bool:
    """Check if a shell command matches any of the glob patterns.

    Splits on ; && || | to check each sub-command independently.
    """
    subcmds = _split_cmd(command.strip())
    for sub in subcmds:
        for pat in patterns:
            if fnmatch(sub, pat):
                return True
    return False


def check_read(path: str, rules: Rules) -> str | None:
    """Check if reading a path is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: secrets baseline > most-specific matching rule (deny wins ties)
    > default allow.
    """
    cwd = os.getcwd()
    resolved = str(Path(path).resolve())

    # 1. Hardcoded secrets baseline (highest priority)
    if _is_secret_path(resolved):
        return f"<error>Read denied: sensitive path {path}</error>"

    # 2. Most-specific matching rule wins; deny wins ties
    allow_match = _most_specific_match(resolved, rules.allow_read, cwd)
    deny_match = _most_specific_match(resolved, rules.deny_read, cwd)
    if allow_match is not None and deny_match is not None:
        if _pattern_specificity(allow_match) > _pattern_specificity(deny_match):
            return None  # allow is more specific
        return f"<error>Read denied by denyRead rule: {deny_match}</error>"
    if deny_match is not None:
        return f"<error>Read denied by denyRead rule: {deny_match}</error>"

    # 3. Default: allow (read is default-open; allow_read only acts as escape
    # hatch against a broad deny_read)
    return None


def check_write(path: str, rules: Rules) -> str | None:
    """Check if writing to a path is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: secrets baseline > most-specific matching rule (deny wins ties).
    When allow_write is defined, non-matching paths are denied (default-deny
    under an allow-list); otherwise default-allow.
    """
    cwd = os.getcwd()
    resolved = str(Path(path).resolve())

    # 1. Hardcoded secrets baseline (highest priority)
    if _is_secret_path(resolved):
        return f"<error>Write denied: sensitive path {path}</error>"

    # 2. Most-specific matching rule wins; deny wins ties
    allow_match = _most_specific_match(resolved, rules.allow_write, cwd)
    deny_match = _most_specific_match(resolved, rules.deny_write, cwd)
    if allow_match is not None and deny_match is not None:
        if _pattern_specificity(allow_match) > _pattern_specificity(deny_match):
            return None  # allow is more specific (escape hatch for broad deny)
        return f"<error>Write denied by denyWrite rule: {deny_match}</error>"
    if deny_match is not None:
        return f"<error>Write denied by denyWrite rule: {deny_match}</error>"
    if allow_match is not None:
        return None

    # 3. No rule matches. If an allow_write list exists, this path isn't in it
    # → deny (allow-list semantics). Otherwise default-allow.
    if rules.allow_write:
        return f"<error>Write denied (not in allowWrite): {path}</error>"
    return None


def check_cmd(command: str, rules: Rules) -> str | None:
    """Check if executing a command is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: deny rules > shell expansion detection > default allow.
    """
    # 1. Deny rules always win
    if _match_cmd(command, rules.deny_cmd):
        return f"<error>Command denied by denyCmd rule: {command[:80]}"

    # 2. Block dangerous shell metacharacters that bypass pattern matching
    if _has_shell_expansion(command):
        return f"<error>Command denied: contains shell command substitution: {command[:80]}"

    # 3. Default: allow
    return None
