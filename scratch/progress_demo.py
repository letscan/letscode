#!/usr/bin/env python3
"""Python equivalent of scratch/progress_demo.sh — a fake package installer.

Three phases, identical separator semantics to the bash version:
  1. Multi-line \\n output  (resolving dependencies)
  2. \\r progress bar       (downloading)
  3. Multi-line \\n output  (installing/verifying)

Each line is emitted ~0.1s apart. flush=True is essential: without it Python
block-buffers stdout when not attached to a TTY, and the Bash tool's PTY would
see nothing until the buffer fills.

Under letscode:
    letscode -v "run python scratch/progress_demo.py"

Raw byte inspection (\\r shows as ^M):
    python3 scratch/progress_demo.py | cat -v
"""

import sys
import time

DELAY = 0.1


def emit(line: str) -> None:
    """Emit a \\n-terminated line and flush immediately."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    time.sleep(DELAY)


def main() -> int:
    # --- Phase 1: multi-line resolved-dependency log ---
    emit("Resolving dependencies...")
    for pkg in (
        "core-2.4.1",
        "netlib-0.8.3",
        "parser-1.2.0",
        "codec-3.1.5",
        "utils-0.9.2",
        "logging-1.4.0",
        "crypto-2.0.1",
        "scheduler-0.3.7",
    ):
        emit(f"  ├─ {pkg}")
    emit("Resolved 8 packages, planning install order...")

    # --- Phase 2: \r-updated progress bar (in-place) ---
    # No trailing newline while iterating: each update overwrites the line.
    total = 20
    for i in range(1, total + 1):
        pct = i * 100 // total
        filled = "█" * i
        empty = "░" * (total - i)
        sys.stdout.write(f"\rDownloading [{filled}{empty}] {pct:3d}%")
        sys.stdout.flush()
        time.sleep(DELAY)
    sys.stdout.write("\n")
    sys.stdout.flush()

    # --- Phase 3: multi-line install/verify log ---
    for pkg in (
        "core-2.4.1",
        "netlib-0.8.3",
        "parser-1.2.0",
        "codec-3.1.5",
        "utils-0.9.2",
        "logging-1.4.0",
        "crypto-2.0.1",
        "scheduler-0.3.7",
    ):
        emit(f"Installing {pkg}... done")
    emit("Verifying signatures... ok")
    emit("Cleaning up temporary files... done")
    emit("All packages installed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
