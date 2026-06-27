#!/usr/bin/env bash
# Simulates a package installer with three phases:
#   1. Multi-line \n output  (resolving dependencies)
#   2. \r progress bar       (downloading)
#   3. Multi-line \n output  (installing/verifying)
#
# Each line is emitted ~0.1s apart so the CLI streaming window is observable.
#
# Under letscode:
#   letscode -v "run scratch/progress_demo.sh"
#
# Raw byte inspection (the \r shows as ^M):
#   bash scratch/progress_demo.sh | cat -v

set -u
DELAY=0.1

emit() { printf '%s\n' "$1"; sleep "$DELAY"; }

# --- Phase 1: multi-line resolved-dependency log ---
emit "Resolving dependencies..."
emit "  ├─ core-2.4.1"
emit "  ├─ netlib-0.8.3"
emit "  ├─ parser-1.2.0"
emit "  ├─ codec-3.1.5"
emit "  ├─ utils-0.9.2"
emit "  ├─ logging-1.4.0"
emit "  ├─ crypto-2.0.1"
emit "  └─ scheduler-0.3.7"
emit "Resolved 8 packages, planning install order..."

# --- Phase 2: \r-updated progress bar (in-place) ---
total=20
for i in $(seq 1 "$total"); do
    pct=$(( i * 100 / total ))
    filled=$(printf '█%.0s' $(seq 1 "$i"))
    empty=$(printf '░%.0s' $(seq 1 $(( total - i ))))
    printf '\rDownloading [%s%s] %3d%%' "$filled" "$empty" "$pct"
    sleep "$DELAY"
done
printf '\n'

# --- Phase 3: multi-line install/verify log ---
emit "Installing core-2.4.1... done"
emit "Installing netlib-0.8.3... done"
emit "Installing parser-1.2.0... done"
emit "Installing codec-3.1.5... done"
emit "Installing utils-0.9.2... done"
emit "Installing logging-1.4.0... done"
emit "Installing crypto-2.0.1... done"
emit "Installing scheduler-0.3.7... done"
emit "Verifying signatures... ok"
emit "Cleaning up temporary files... done"
emit "All packages installed successfully."
