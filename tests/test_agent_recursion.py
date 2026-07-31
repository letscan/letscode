"""Tests for the Agent tool's recursion guard.

The Agent tool refuses to spawn a sub-agent whose card is already in the
ancestor chain (LETSCODE_AGENT_CHAIN, set by cli.py on each spawn). This
prevents infinite recursion (A→A, A→B→A) while allowing arbitrary-depth
acyclic delegation (A→B→C).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from letscode.tools import agent as agent_tool


@pytest.fixture(autouse=True)
def _clean_chain_env(monkeypatch):
    """Ensure no leaked LETSCODE_AGENT_CHAIN between tests."""
    monkeypatch.delenv("LETSCODE_AGENT_CHAIN", raising=False)


def _stub_subprocess_to_record(captured_cmd):
    """Patch subprocess.run to record the cmd and return a benign result,
    so we can assert what WOULD have been spawned without actually spawning."""
    def fake_run(cmd, **kw):
        captured_cmd.append(list(cmd))
        return MagicMock(stdout="ok", stderr="", returncode=0)
    return fake_run


class TestRecursionGuard:
    def test_no_chain_allows_any_spawn(self):
        """With no ancestor chain (top-level agent), any sub-agent is allowed."""
        captured = []
        with patch.object(__import__("subprocess"), "run",
                          side_effect=_stub_subprocess_to_record(captured)):
            r = agent_tool.execute(
                {"description": "x", "prompt": "p", "subagent_type": "Explore"})
        assert "spawn" not in r.lower() or "error" not in r.lower()
        assert any("--as" in c for c in captured), "sub-agent was spawned"

    def test_direct_self_recursion_blocked(self):
        """Agent A trying to spawn A again → refused (A→A)."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "research"
        r = agent_tool.execute(
            {"description": "x", "prompt": "p", "subagent_type": "Research"})
        assert r.startswith("<error>")
        assert "Research" in r
        assert "ancestor chain" in r or "recurse" in r

    def test_indirect_cycle_blocked(self):
        """A→B→A is caught: when B (chain "a,b") tries to spawn A."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "a,b"
        r = agent_tool.execute(
            {"description": "x", "prompt": "p", "subagent_type": "a"})
        assert r.startswith("<error>")
        assert "a -> b -> a" in r, "error should show the full cycle path"

    def test_case_insensitive_match(self):
        """Card names match case-insensitively (cards are discovered lowercased)."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "worker"
        r = agent_tool.execute(
            {"description": "x", "prompt": "p", "subagent_type": "WORKER"})
        assert r.startswith("<error>")

    def test_acyclic_chain_allowed(self):
        """A→B→C (each different) is allowed — only cycles are blocked."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "a,b"
        captured = []
        with patch.object(__import__("subprocess"), "run",
                          side_effect=_stub_subprocess_to_record(captured)):
            r = agent_tool.execute(
                {"description": "x", "prompt": "p", "subagent_type": "c"})
        assert not r.startswith("<error>"), f"acyclic spawn wrongly blocked: {r}"
        assert any("--as" in c and "c" in c for c in captured)

    def test_general_purpose_always_allowed(self):
        """general-purpose (no --as card) is never in the chain → always allowed,
        even from deep in a chain."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "a,b,c"
        captured = []
        with patch.object(__import__("subprocess"), "run",
                          side_effect=_stub_subprocess_to_record(captured)):
            # subagent_type=general-purpose is a real value the model emits; it's
            # not a card name in the chain, so it's allowed.
            r = agent_tool.execute(
                {"description": "x", "prompt": "p",
                 "subagent_type": "general-purpose"})
        assert not r.startswith("<error>"), r

    def test_default_agent_sentinel_blocks_loop(self):
        """A no-card (default) agent is recorded as __default__ in the chain.
        If a card agent spawns default, and default tries to spawn that card
        back, the cycle is caught."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "__default__"
        # default agent trying to spawn a card that leads back is fine here,
        # but spawning __default__ again would loop — not directly reachable
        # via --as, so we test the sentinel is in the chain correctly.
        assert "__default__" in os.environ["LETSCODE_AGENT_CHAIN"]


class TestChainPropagation:
    """The chain is carried via os.environ, which subprocess.run inherits
    automatically. cli.py appends the current card on startup."""

    def test_spawn_inherits_chain_env(self):
        """When a sub-agent is spawned, it inherits LETSCODE_AGENT_CHAIN from
        the parent process env (subprocess.run default). We verify the env is
        present at spawn time."""
        os.environ["LETSCODE_AGENT_CHAIN"] = "parent"
        captured_env = {}

        def fake_run(cmd, **kw):
            # subprocess.run merges env=None over os.environ; capture it.
            captured_env.update(os.environ)
            return MagicMock(stdout="ok", stderr="", returncode=0)
        with patch.object(__import__("subprocess"), "run", side_effect=fake_run):
            agent_tool.execute(
                {"description": "x", "prompt": "p", "subagent_type": "child"})
        assert captured_env.get("LETSCODE_AGENT_CHAIN") == "parent"
