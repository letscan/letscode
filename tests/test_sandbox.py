"""Tests for sandbox.is_likely_sandbox_denied — the post-command heuristic.

Detecting that a command was blocked by the sandbox has no deterministic
signal on either platform: ``sandbox-exec`` returns 0 itself, the intercepted
syscall surfaces inside the child as EPERM, and — critically — a command chain
like ``denied_write; echo done`` exits 0 while the denied operation's error
text still appears in the output. So the classifier keys purely on the
presence of a denial keyword in the combined output (stdout+stderr),
INDEPENDENT of exit code. Adapted from codex-rs/core/src/exec.rs (which gates
on exit_code != 0); we drop that gate because agent-issued command chains
frequently mask the denied operation's non-zero exit behind a trailing
successful command.

False positives (a successful command whose output coincidentally contains
one of these strings) are accepted: they are rare, and the downstream probe +
user popup tolerates them.
"""

import pytest

from letscode.sandbox import (
    _SANDBOX_DENIED_KEYWORDS,
    is_likely_sandbox_denied,
)


class TestKeywordDetection:
    """Any of the 7 denial keywords (case-insensitive) in the output → True."""

    @pytest.mark.parametrize("keyword", list(_SANDBOX_DENIED_KEYWORDS))
    def test_each_keyword_matches(self, keyword):
        assert is_likely_sandbox_denied(
            output=f"error: {keyword} somewhere",
        ) is True

    def test_case_insensitive(self):
        # EPERM's canonical text is title-case "Operation not permitted".
        assert is_likely_sandbox_denied(output="Operation Not Permitted") is True
        assert is_likely_sandbox_denied(output="OPERATION NOT PERMITTED") is True

    def test_keyword_amid_other_text(self):
        assert is_likely_sandbox_denied(
            output="doing thing 1\ndoing thing 2\n"
                   "fatal: permission denied for repo",
        ) is True


class TestExitCodeIndependent:
    """The whole point of dropping the exit_code gate: a command whose overall
    returncode is 0 (because a trailing ``echo``/``||`` masked the failure)
    still counts as denied when the output carries the denial signature."""

    def test_exit_zero_with_keyword_still_denied(self):
        # Mirrors the real failure mode: agent writes
        #   `echo x > /denied 2>&1; echo "Exit code: $?"`
        # — the write is denied (EPERM in output) but the trailing echo
        # succeeds, so the chain exits 0. Keyword detection must still fire.
        output = (
            'Exit code: 1\n'
            'zsh:1: operation not permitted: /etc/hosts\n'
        )
        assert is_likely_sandbox_denied(output=output) is True

    def test_exit_zero_no_keyword_not_denied(self):
        assert is_likely_sandbox_denied(output="all good") is False


class TestNoFalseNegatives:
    """Realistic command outputs that must be flagged."""

    def test_write_denied(self):
        assert is_likely_sandbox_denied(
            output="/etc/hosts: Operation not permitted",
        ) is True

    def test_ln_denied(self):
        # The exact TC[3] shape from the bug report.
        output = (
            "---\nln: ../tmp_link: Operation not permitted\n---\n"
            "zsh:2: operation not permitted: /outside/security_test.txt"
        )
        assert is_likely_sandbox_denied(output=output) is True

    def test_read_only_filesystem(self):
        assert is_likely_sandbox_denied(
            output="touch: /readonly/x: Read-only file system",
        ) is True


class TestAcceptableFalsePositives:
    """Successful commands that happen to contain a keyword are flagged —
    accepted by design. These tests document the trade-off, not a bug."""

    def test_grep_output_containing_keyword(self):
        # A successful grep for "permission denied" in a log file — the match
        # text contains the keyword. Flagged. Rare in practice; probe filters.
        assert is_likely_sandbox_denied(
            output="auth.log:42:permission denied for user guest",
        ) is True


class TestEmptyAndEdgeCases:
    def test_empty_output(self):
        assert is_likely_sandbox_denied(output="") is False

    def test_none_output_treated_as_empty(self):
        # Defensive: caller passing None shouldn't crash.
        assert is_likely_sandbox_denied(output=None) is False  # type: ignore[arg-type]

    def test_no_keyword_in_realistic_failure(self):
        # A genuine build failure (gcc errors) must NOT be flagged.
        realistic = (
            "main.c:10:5: error: use of undeclared identifier 'foo'\n"
            "1 error generated."
        )
        assert is_likely_sandbox_denied(output=realistic) is False
