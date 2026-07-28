#!/bin/bash
# verify.sh — the fixed acceptance contract.
#
# Invoked by devhard_loop.sh. It checks that the Tester-produced acceptance
# script (run_test.sh) exists, then executes it and transparently returns its
# exit code: 0 = acceptance passed, non-zero = failed.
#
# This script's content is FIXED — it is part of the harness, not generated
# per-task. The per-task knowledge (which test command to run) lives in
# run_test.sh, produced by the Tester sub-agent. This separation keeps the
# loop control deterministic (no LLM here) while supporting any language or
# test framework (whatever run_test.sh invokes).
#
# Working directory: the project root (same cwd as devhard_loop.sh).

set -eu

if [ ! -f run_test.sh ]; then
  echo "run_test.sh not found — Tester did not produce an acceptance script."
  echo "Cannot verify. Ensure the Tester writes run_test.sh in the project root."
  exit 1
fi

echo "Running acceptance script (run_test.sh)..."
# Execute via bash so run_test.sh need not be chmod +x (shebang optional).
exec bash run_test.sh
