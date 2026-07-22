"""AgentCard hooks — run scripts at the start/end of an agent run.

A card may declare ``onAgentStart`` and/or ``onAgentEnd`` in its frontmatter,
each pointing to a shell script path. The harness executes them at the
corresponding lifecycle points of a **big turn** (one complete ``run_agent``
invocation — potentially many LLM API calls + tool executions).

Return-code contract (shared by both hooks):

- **0** — stdout is injected as a user message the LLM sees.
- **2** — abort. For onAgentStart the agent loop is skipped entirely.
  stdout is treated as an error message (shown to user / emitted as error).
  For onAgentEnd, same: stdout is the error reason, run exit code → 1.
- **other non-zero** — treated as an unexpected script failure. stdout (or
  stderr if stdout is empty) is shown as a warning. For onAgentStart the
  agent loop still runs (best-effort). For onAgentEnd the run exit code → 1.

Both hooks receive a JSON summary on stdin describing the run::

    {"turn": N, "tool_calls": [{"name": "Bash", "success": true}, ...]}

For onAgentStart, ``turn`` is 0 and ``tool_calls`` is empty (the run hasn't
started yet). For onAgentEnd, they reflect the full run.

Scripts run in the project cwd under the card's sandbox preset. If a declared
script path doesn't exist, the hook is silently skipped (not an error).
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass


# Sentinel return codes with dedicated semantics.
HOOK_ABORT = 2  # script explicitly requests abort (onStart: skip agent loop)


@dataclass
class HookResult:
    """Result of running a hook script.

    - ``stdout``: the script's stdout (to inject as context or show as error).
    - ``returncode``: 0 = success, HOOK_ABORT (2) = abort, other = failure.
    - ``skipped``: True if the script path didn't exist (not an error).
    """
    stdout: str
    returncode: int
    skipped: bool = False

    @property
    def aborted(self) -> bool:
        """True when the script explicitly requested abort (exit code 2)."""
        return self.returncode == HOOK_ABORT

    @property
    def ok(self) -> bool:
        """True when the script succeeded (exit code 0) and wasn't skipped."""
        return self.returncode == 0 and not self.skipped


def run_hook(
    script_path: str,
    stdin_data: str,
    cwd: str,
    preset: str = "default",
    sandbox: bool = True,
    timeout: int = 120,
) -> HookResult:
    """Run a hook script and return its output.

    ``script_path`` is executed via ``[shell, script_path]`` in ``cwd``,
    optionally wrapped by the macOS Seatbelt sandbox. stdin receives
    ``stdin_data`` (the run JSON). If the path doesn't exist, returns a
    ``skipped`` result rather than raising.
    """
    # Resolve relative to cwd; skip silently if the script doesn't exist.
    full_path = script_path if os.path.isabs(script_path) else os.path.join(cwd, script_path)
    if not os.path.isfile(full_path):
        return HookResult(stdout="", returncode=0, skipped=True)

    shell = os.environ.get("SHELL", "/bin/bash")
    cmd = [shell, full_path]

    sandbox_active = (
        sandbox and preset in ("safe", "default", "risk")
        and shutil.which("sandbox-exec")
    )
    if sandbox_active:
        from .sandbox import wrap_command
        cmd = wrap_command(cmd, cwd, preset)

    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return HookResult(stdout="", returncode=124)
    except Exception:
        return HookResult(stdout="", returncode=1)

    return HookResult(stdout=result.stdout.strip(), returncode=result.returncode)


def serialize_run(turn: int, tool_calls: list[dict]) -> str:
    """Serialize the run's info for the hook script's stdin."""
    return json.dumps({"turn": turn, "tool_calls": tool_calls})
