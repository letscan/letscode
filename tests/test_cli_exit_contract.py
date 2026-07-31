"""Integration tests for the CLI exit-code / process contract (School A).

Contract (see docs/cli-exit-code-survey.md): when an agent run is cancelled
mid-flight, letscode must NOT leave stdout empty. It exits 0 (interrupt is not
a process-level failure — a parent like gs / the Agent tool shouldn't see a
spurious non-zero) AND emits a one-line termination notice on stdout so a
capturing parent gets *something* to feed back, not an empty pipe.

The prior bug was the worst-of-both: exit 0 + empty stdout.

These are subprocess-level tests because the contract IS about process
behavior (exit code + stdout). A SIGINT delivered to a running letscode must
produce the notice. We use a prompt that keeps the agent busy long enough for
the signal to land mid-run (a no-MCP Research run still takes a couple seconds
to stream).
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config.json"


def _has_config():
    return CONFIG.is_file()


@pytest.mark.skipif(not _has_config(), reason="config.json not present")
class TestCancelExitContract:
    def test_sigint_emits_stdout_notice_and_exit0(self):
        """SIGINT mid-run → exit 0 + a termination notice on stdout (not empty).

        This is the load-bearing contract assertion: a parent capturing stdout
        must NOT get an empty pipe on cancellation. It should get a notice it
        can feed back as data (School A).
        """
        # A prompt that keeps the agent streaming long enough for SIGINT to
        # land mid-run. Even --no-mcp needs a round-trip to the model.
        proc = subprocess.Popen(
            [sys.executable, "-m", "letscode", "--as", "Research", "--no-mcp",
             "-c", str(CONFIG),
             "Write a long, detailed essay about the history of computing. "
             "Take your time and be thorough."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO),
            # New process group so we can signal just this process.
            start_new_session=False,
        )
        try:
            # Give it time to connect + start streaming.
            time.sleep(4)
            if proc.poll() is not None:
                pytest.skip("agent finished before SIGINT could land")
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("process did not exit after SIGINT")

        # Contract assertions:
        assert proc.returncode == 0, (
            f"cancelled run must exit 0 (interrupt is not a process failure); "
            f"got exit {proc.returncode}. stderr:\n{stderr[-500:]}"
        )
        assert "Agent terminated early" in stdout or "interrupted" in stdout.lower(), (
            "stdout must carry a termination notice on cancellation, not be "
            f"empty. stdout tail:\n{stdout[-300:]!r}"
        )
