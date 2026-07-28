#!/bin/bash
# devhard_loop.sh — the deterministic Worker-Tester loop controller.
#
# This is DevHard's onAgentEnd hook. After the DevHard orchestrator's agent
# loop completes (which ran the Plan sub-agent to produce plan.md), THIS script
# takes over as the loop body — it is the sole driver of the Worker-Tester
# cycle. The LLM orchestrator does NOT spawn Worker or Tester; the hook does,
# deterministically.
#
# Flow:
#   1. spawn Tester  → writes test cases + run_test.sh (acceptance criteria)
#   2. spawn Worker  → writes implementation (per plan.md)
#   3. loop:
#        verify.sh → run_test.sh → exit code
#        pass   → "All tests passed." → exit 0
#        fail   → spawn Worker to fix (reads failure output, must not touch tests)
#                 → re-verify
#   4. max iterations reached → exit 2 (abort)
#
# Why the hook owns the loop (not the LLM): pass/fail judgment is a shell
# command's exit code (verify.sh → run_test.sh), not an LLM's self-report.
# The LLM cannot influence whether the loop continues — only exit codes can.
#
# Environment (exported by letscode before running the hook):
#   LETSCODE_STATE  — shared state JSON path (--state), for iteration tracking
#   LETSCODE_CONFIG — config file in use (--config); required to spawn sub-agents
#   LETSCODE_PYTHON — interpreter for `python -m letscode` (the venv with letscode)
#
# stdin: {"turn": N, "tool_calls": [...]}  (ignored — we drive the loop)
#
# Exit codes (onAgentEnd contract):
#   0 — success (stdout shown to user); includes "reached max iterations"
#   2 — abort (run marked as error)

set -eu

MAX_ITERATIONS=5
STATE="${LETSCODE_STATE:-}"
PY="${LETSCODE_PYTHON:-python3}"
# verify.sh is a sibling of this script in builtin_agents/hooks/.
# run_hook executes us by absolute path, so $0 is reliable.
VERIFY_SH="$(dirname "$0")/verify.sh"

# --- Pre-flight: need config to spawn sub-agents with a real model/API ---
if [ -z "${LETSCODE_CONFIG:-}" ]; then
  echo "LETSCODE_CONFIG is not set — cannot spawn Tester/Worker sub-agents."
  echo "Run letscode with -c <config> so the hook can chain sub-agents."
  exit 2
fi

if [ ! -f "$VERIFY_SH" ]; then
  echo "verify.sh not found at $VERIFY_SH — harness installation is broken."
  exit 2
fi

# --- Helpers ---

read_iter() {
  # Read iteration count from state.json (default 0).
  if [ -n "$STATE" ] && [ -f "$STATE" ]; then
    "$PY" -c "import json; print(json.load(open('$STATE')).get('iteration', 0))" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

write_state() {
  # Update state.json: iteration + last verify output (passed via stdin).
  if [ -n "$STATE" ]; then
    mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
    local iter="$1"
    "$PY" -c "
import json, sys
try:
    d = json.load(open('$STATE'))
except Exception:
    d = {}
d['iteration'] = $iter
d['last_test_output'] = sys.stdin.read()
json.dump(d, open('$STATE', 'w'))
" 2>/dev/null || true
  fi
}

spawn_agent() {
  # spawn_agent <card> <prompt>
  # Spawns a letscode sub-agent (Tester/Worker). Shares state + config with
  # the orchestrator. Build the command as an array to avoid shell
  # word-splitting issues (zsh doesn't word-split unquoted ${...:+...}, which
  # would merge "--state <path>" into a single arg and break argparse).
  local card="$1"; shift
  local prompt="$1"
  local -a cmd
  cmd=("$PY" -m letscode --as "$card" --config "$LETSCODE_CONFIG")
  if [ -n "$STATE" ]; then
    cmd+=(--state "$STATE")
  fi
  cmd+=("$prompt")
  "${cmd[@]}" 2>&1 || true
}

run_verify() {
  # Run verify.sh, capture output. Sets TEST_OUTPUT; returns its exit code.
  TEST_OUTPUT=$(bash "$VERIFY_SH" 2>&1) || return 1
  return 0
}

# --- Read plan.md for prompts (Plan produced it in the agent loop) ---
PLAN=""
if [ -f plan.md ]; then
  PLAN=$(cat plan.md)
else
  echo "Warning: plan.md not found. Sub-agents will work from the task prompt only."
fi

# ── Phase 1: Tester writes tests + run_test.sh (TDD: tests first) ──
echo "=== Spawning Tester to write acceptance criteria ==="
spawn_agent Tester "Read plan.md (below) and the original task. Write test cases AND a run_test.sh script in the project root. run_test.sh must exit 0 when all tests pass, non-zero otherwise. Do NOT write implementation code — only tests and run_test.sh.

Plan:
$PLAN"

# ── Phase 2: Worker writes initial implementation ──
echo "=== Spawning Worker to implement ==="
spawn_agent Worker "Read plan.md (below) and write the implementation so that run_test.sh exits 0. Do NOT modify run_test.sh or any test files — they are the acceptance criteria.

Plan:
$PLAN"

# ── Phase 3: verify → Tester-review → Worker-fix loop ──
# Each iteration: verify first. On failure, Tester reviews the Worker's changes
# (detects cheating: weakened tests, deleted assertions, test files modified by
# Worker; and tightens coverage: adds tests for untested edge cases the failure
# revealed). Then Worker fixes against the updated, trustworthy tests. This
# closed loop prevents Worker from gaming the acceptance criteria.
ITER=$(read_iter)

while [ "$ITER" -lt "$MAX_ITERATIONS" ]; do
  echo "=== Verification (iteration $ITER) ==="
  if run_verify; then
    # All tests pass — but do a final Tester review to catch cheating that
    # didn't cause a test failure (e.g. Worker deleted a test entirely).
    echo "=== Tests passed. Spawning Tester for anti-cheat review ==="
    spawn_agent Tester "The acceptance tests all passed. Do a final review:
1. Check that no test files were deleted or weakened by the Worker (compare against the plan's acceptance criteria).
2. If you find tampering, restore the original tests.
3. If all tests are intact and faithful, do nothing and exit.
Do NOT write or modify implementation code." >/dev/null
    # Re-verify after the anti-cheat check (Tester may have restored tests).
    if run_verify; then
      echo "All tests passed."
      write_state "$ITER" <<< "$TEST_OUTPUT"
      exit 0
    fi
    # Tester found cheating and restored tests → tests fail again → fall through.
  fi

  # Tests failed — extract failure lines.
  ITER=$((ITER + 1))
  write_state "$ITER" <<< "$TEST_OUTPUT"

  FAILURES=$(echo "$TEST_OUTPUT" | grep -E '^(FAILED|ERROR|FAIL|---|AssertionError|Test Suite)' | head -15)
  if [ -z "$FAILURES" ]; then
    FAILURES=$(echo "$TEST_OUTPUT" | tail -15)
  fi

  # Tester reviews and tightens tests BEFORE Worker fixes.
  echo "=== Tests failed (iteration $ITER). Spawning Tester to review ==="
  spawn_agent Tester "The acceptance tests failed. Review the situation:
1. Check whether the Worker modified any test files or run_test.sh (cheating). If so, restore them.
2. The failure may reveal an untested edge case or a gap in coverage. Add or strengthen tests if needed.
3. Do NOT fix the implementation — that's the Worker's job. Only ensure the tests are correct, complete, and not tampered with.
Failure output:
$FAILURES" >/dev/null

  # Re-extract failures in case Tester changed the tests.
  if run_verify; then
    echo "Tester tightened tests and they now pass. All tests passed (iteration $ITER)."
    write_state "$ITER" <<< "$TEST_OUTPUT"
    exit 0
  fi
  FAILURES=$(echo "$TEST_OUTPUT" | grep -E '^(FAILED|ERROR|FAIL|---|AssertionError|Test Suite)' | head -15)
  if [ -z "$FAILURES" ]; then
    FAILURES=$(echo "$TEST_OUTPUT" | tail -15)
  fi

  # Worker fixes against the updated, trustworthy tests.
  echo "=== Spawning Worker to fix (iteration $ITER) ==="
  spawn_agent Worker "The acceptance tests (run_test.sh) are failing. Fix the IMPLEMENTATION so they pass. Do NOT modify run_test.sh or any test files — they are the acceptance criteria. Failure output:
$FAILURES"
done

# ── Max iterations reached ──
echo "Reached max iterations ($MAX_ITERATIONS). Tests still failing."
echo "Last verify output:"
echo "$TEST_OUTPUT" | tail -20
write_state "$ITER" <<< "$TEST_OUTPUT"
exit 2
