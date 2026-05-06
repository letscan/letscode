"""Fine-grained access rules engine for path and command restrictions."""

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

_SECRET_COMPONENTS = frozenset(
    [".ssh", ".aws", ".gnupg", ".netrc", "credentials"]
)

_SECRET_FILENAMES = frozenset(
    [".env", ".envrc"]
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
        return str(Path.home() / pattern[2:]), False
    if pattern.startswith("./"):
        return os.path.normpath(os.path.join(cwd, pattern[2:])), False
    if pattern.startswith("/"):
        return os.path.normpath(pattern), False
    return os.path.normpath(os.path.join(cwd, pattern)), False


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


def _match_cmd(command: str, patterns: list[str]) -> bool:
    """Check if a shell command matches any of the glob patterns.

    Splits on ; && || | to check each sub-command independently.
    """
    # Split command into individual sub-commands
    subcmds = _split_cmd(command.strip())
    for sub in subcmds:
        for pat in patterns:
            if fnmatch(sub, pat):
                return True
    return False


def _split_cmd(command: str) -> list[str]:
    """Split a shell command into sub-commands by ; && || |."""
    parts = []
    current = []
    i = 0
    while i < len(command):
        c = command[i]
        if c in (';', '|', '&'):
            if current:
                parts.append("".join(current).strip())
                current = []
            # Skip && || sequences
            if i + 1 < len(command) and command[i + 1] in ('&', '|'):
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


def check_read(path: str, rules: Rules) -> str | None:
    """Check if reading a path is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: deny rules > secrets baseline > allow rules > default allow.
    """
    cwd = os.getcwd()
    resolved = str(Path(path).resolve())

    # 1. Deny rules always win
    if _match_path(resolved, rules.deny_read, cwd):
        return f"<error>Read denied by denyRead rule: {path}</error>"

    # 2. Hardcoded secrets baseline
    if _is_secret_path(resolved):
        return f"<error>Read denied: sensitive path {path}</error>"

    # 3. Allow rules (escape hatch for broad deny rules)
    if rules.allow_read and _match_path(resolved, rules.allow_read, cwd):
        return None

    # 4. Default: allow
    return None


def check_write(path: str, rules: Rules) -> str | None:
    """Check if writing to a path is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: deny rules > secrets baseline > allow rules (default-deny when allow exists).
    """
    cwd = os.getcwd()
    resolved = str(Path(path).resolve())

    # 1. Deny rules always win
    if _match_path(resolved, rules.deny_write, cwd):
        return f"<error>Write denied by denyWrite rule: {path}</error>"

    # 2. Hardcoded secrets baseline
    if _is_secret_path(resolved):
        return f"<error>Write denied: sensitive path {path}</error>"

    # 3. If allow rules are defined, only matching paths are writable
    if rules.allow_write:
        if _match_path(resolved, rules.allow_write, cwd):
            return None
        return f"<error>Write denied (not in allowWrite): {path}</error>"

    # 4. No allow rules → allow all
    return None


def check_cmd(command: str, rules: Rules) -> str | None:
    """Check if executing a command is allowed.

    Returns None if allowed, or an error message if denied.
    Priority: deny rules > default allow.
    """
    # 1. Deny rules always win
    if _match_cmd(command, rules.deny_cmd):
        return f"<error>Command denied by denyCmd rule: {command[:80]}"

    # 2. Default: allow
    return None
