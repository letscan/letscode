"""macOS Seatbelt sandbox for Bash tool subprocess isolation."""

import os
import tempfile
from pathlib import Path

# Shared blocks
_NETWORK = "(allow network*)"
_PROCESS = "(allow process-exec)\n(allow process-fork)"
_SYSTEM_READ = """\
(allow file-read* (subpath "/usr"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/bin"))
(allow file-read* (subpath "/sbin"))
(allow file-read* (subpath "/lib"))
(allow file-read* (subpath "/etc"))
(allow file-read* (subpath "/private/etc"))
(allow file-read* (subpath "/tmp"))
(allow file-read* (subpath "/var"))
(allow file-read* (subpath "/private/tmp"))
(allow file-read* (subpath "/private/var"))
(allow file-read* (subpath "/opt"))
(allow file-read* (subpath "/Library"))
(allow file-read-metadata)"""

_SECRETS_DENY_READ = """\
; Deny reading secrets
(deny file-read* (regex #"\\.ssh(/|$)"))
(deny file-read* (regex #"\\.aws(/|$)"))
(deny file-read* (regex #"\\.gnupg(/|$)"))
(deny file-read* (regex #"\\.env$"))
(deny file-read* (regex #"/credentials"))
(deny file-read* (regex #"/\\.netrc$"))"""

_SECRETS_DENY_WRITE = """\
; Deny writing secrets (even in allowed paths)
(deny file-write* (regex #"\\.ssh(/|$)"))
(deny file-write* (regex #"\\.aws(/|$)"))
(deny file-write* (regex #"\\.gnupg(/|$)"))
(deny file-write* (regex #"\\.env$"))
(deny file-write* (regex #"/credentials"))
(deny file-write* (regex #"\\.(bashrc|zshrc|bash_profile|bash_logout|profile|zprofile|zshenv)$"))
(deny file-write* (regex #"\\.git/hooks(/|$)"))"""

# safe: read-only everywhere (except secrets), no writes
_SAFE = f"""\
(version 1)
(deny default)
(import "bsd.sb")

; Read — system + home + workspace
{_SYSTEM_READ}
(allow file-read* (subpath (param "HOME")))
(allow file-read* (subpath (param "WORKDIR")))
{_SECRETS_DENY_READ}

; No writes at all

{_NETWORK}
{_PROCESS}
{_SECRETS_DENY_WRITE}
"""

# default: read-only + workspace writable (secrets unreadable)
_DEFAULT = f"""\
(version 1)
(deny default)
(import "bsd.sb")

; Read — system + home + workspace
{_SYSTEM_READ}
(allow file-read* (subpath (param "HOME")))
(allow file-read* (subpath (param "WORKDIR")))
{_SECRETS_DENY_READ}

; Write — workspace + tmp only
(allow file-write* (subpath (param "WORKDIR")))
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/var/folders"))
(allow file-write* (subpath "/private/var/folders"))

{_NETWORK}
{_PROCESS}
{_SECRETS_DENY_WRITE}
"""

# risk: full filesystem read/write; secrets readable but writing is denied
_RISK = f"""\
(version 1)
(deny default)
(import "bsd.sb")

; Full filesystem read/write
(allow file-read*)
(allow file-write*)

{_NETWORK}
{_PROCESS}
{_SECRETS_DENY_WRITE}
"""

PROFILES = {
    "safe": _SAFE,
    "default": _DEFAULT,
    "risk": _RISK,
}

VALID_PRESETS = set(PROFILES.keys())

_PRESET_INFO = [
    {"id": "safe", "name": "Safe", "description": "No writes allowed"},
    {"id": "default", "name": "Default", "description": "Workspace writable"},
    {"id": "risk", "name": "Risk", "description": "Full filesystem read/write"},
]


def list_presets() -> list[dict]:
    """Return list of available sandbox presets with id, name, and description."""
    return list(_PRESET_INFO)


_cache: dict[str, str] = {}


def get_profile_path(workspace: str, preset: str) -> str:
    """Write the Seatbelt profile to a temp file (cached).

    Returns the path to the profile file.
    """
    resolved = str(Path(workspace).resolve())
    cache_key = f"{resolved}:{preset}"
    if cache_key in _cache and os.path.exists(_cache[cache_key]):
        return _cache[cache_key]

    profile_text = PROFILES[preset]
    tmp_dir = tempfile.gettempdir()
    profile_path = os.path.join(
        tmp_dir, f"letscode-sandbox-{preset}-{hash(resolved) & 0xFFFF:x}.sb"
    )
    fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, profile_text.encode())
    finally:
        os.close(fd)

    _cache[cache_key] = profile_path
    return profile_path


def wrap_command(cmd: list[str], workspace: str, preset: str = "default") -> list[str]:
    """Wrap a command with sandbox-exec using the given preset."""
    profile = get_profile_path(workspace, preset)
    return [
        "sandbox-exec", "-f", profile,
        "-D", f"WORKDIR={workspace}",
        "-D", f"HOME={Path.home()}",
        "--",
    ] + cmd


# ---------------------------------------------------------------------------
# Runtime denial detection (post-command heuristic)
# ---------------------------------------------------------------------------

# Denial signatures searched for (case-insensitive) in command output. These
# are the syscall-level error strings the kernel/shell emits when an operation
# is blocked — by the sandbox at runtime OR by the OS/rules at the tool layer:
#   - "operation not permitted" : EPERM (macOS Seatbelt intercept)
#   - "permission denied"       : EACCES
#   - "read-only file system"   : EROFS (write to a read-only mount)
#   - "seccomp" / "landlock"    : Linux sandbox backends naming themselves
#   - "sandbox"                 : broad catch (sandbox-exec / sandboxd)
#   - "failed to write file"    : apply_patch / tool-level write failure
# Adapted from codex's is_likely_sandbox_denied (codex-rs/core/src/exec.rs).
_SANDBOX_DENIED_KEYWORDS: tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "seccomp",
    "landlock",
    "sandbox",
    "failed to write file",
)


def is_likely_sandbox_denied(*, output: str) -> bool:
    """Heuristic: does this command's output indicate a permission denial?

    There is no deterministic signal for sandbox denial on either platform
    (``sandbox-exec`` returns 0 itself; the intercepted syscall surfaces inside
    the child as EPERM, and a command chain like ``bad; echo done`` masks the
    failure with a trailing success). The exit code is therefore unreliable:
    a denied write often surfaces as ``Operation not permitted`` in the output
    while the overall command still exits 0. We classify purely on the presence
    of a denial keyword in the combined output (stdout+stderr), independent of
    exit code. Adapted from codex's ``is_likely_sandbox_denied`` (which gates
    on exit_code != 0) — we drop that gate because the agent frequently issues
    command chains whose final exit code is 0 even when an intermediate
    operation was denied.

    False positives (a successful command whose output coincidentally contains
    one of these strings) are accepted: they are rare, and the downstream
    probe + user popup tolerates them.
    """
    if not output:
        return False
    lower = output.lower()
    return any(kw in lower for kw in _SANDBOX_DENIED_KEYWORDS)
