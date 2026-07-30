#!/bin/bash
# getsmart_run.sh — GetSmart's onAgentEnd launch hook.
#
# GetSmart-the-agent runs in its own loop and produces a self-contained
# Python program `workflow.py`. This hook's ONLY job is to launch that
# program: put the `gs` library on PYTHONPATH, then run it. The hook does
# NOT interpret the workflow — that's Python's job (wf.run() validates,
# renders the DAG to Mermaid, and executes it topologically).
#
# Why a hook at all (vs. GetSmart calling wf.run() itself)?
#   1. Deterministic execution — the workflow always runs once GetSmart
#      finishes, even if the LLM "forgot" to invoke it. (DevHard's lesson:
#      don't trust the LLM to drive control flow.)
#   2. sandbox=False — the workflow spawns letscode sub-agents that apply
#      their OWN card sandboxes; an outer sandbox-exec wrapper would trip
#      macOS's nested-sandbox_apply ban (the same reason DevHard's hook
#      runs sandbox=False).
#
# Environment (exported by letscode before running the hook):
#   LETSCODE_PYTHON — interpreter that has letscode installed
#   LETSCODE_CONFIG — config in use, forwarded so sub-agents share model/API
#
# Exit codes:
#   workflow.py's own exit code is propagated:
#     - 0     → workflow ran, all nodes ok
#     - 1     → wf.validate() failed (malformed DAG) OR some node failed
#     - 2     → GetSmart never produced workflow.py (didn't deliver)
#
# For a demo agent, exit 1 is honest signal — a malformed/failed workflow is
# GetSmart's failure to produce a usable artifact, not something to paper over.

set -eu

PY="${LETSCODE_PYTHON:-python3}"

# $0 is this script's absolute path (run_hook invokes by abs path).
# hooks/ → parent is GetSmart.assets/, which must be on PYTHONPATH so that
# `from gs import Workflow` resolves `gs` as a subpackage. (Putting gs/ on
# the path directly does NOT work — a dir containing __init__.py isn't
# importable as its own name.)
ASSETS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ASSETS_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

if [ ! -f workflow.py ]; then
  echo "workflow.py not found — GetSmart did not produce its deliverable."
  echo "Expected a self-contained workflow.py in the project root."
  exit 2
fi

# Hand off to the generated program. Its exit code is ours.
exec "$PY" workflow.py
