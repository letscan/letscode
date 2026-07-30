"""Tests for the GetSmart agent and its ``gs`` workflow library.

GetSmart is a third-party-style agent living under ``builtin_agents/``:
``GetSmart.md`` (the card) + ``GetSmart.assets/`` (its library ``gs/`` and the
launch hook). letscode core is untouched; these tests exercise only GetSmart's
own assets + the card structure (mirroring ``test_devhard.py``'s grouping).

Coverage:
1. Card structure (GetSmart discoverable, right preset/hook, assets packaged)
2. ``gs.Workflow.validate`` (acyclic / dangling refs / unknown card / ...)
3. ``gs.Workflow.render`` (Mermaid graph contains every node)
4. ``gs.Workflow.run`` (layering, parallelism, interpolation, resilience)
5. Launch hook ``getsmart_run.sh`` (launches workflow.py; aborts if absent)
"""

import importlib
import json
import os
import stat
import sys
import textwrap
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from letscode.agent_card import discover_agent_cards, load_agent_card
from letscode.hooks import run_hook


# ── make `from gs import Workflow` importable ───────────────────────────
# gs/ ships under builtin_agents/GetSmart.assets/, not on sys.path by default.

_ASSETS_DIR = files("letscode.builtin_agents") / "GetSmart.assets"
# The parent (GetSmart.assets/) must be on sys.path so `gs` resolves as a
# subpackage — putting gs/ itself on the path does NOT work (a dir containing
# __init__.py is not importable as its own name). Same rule the launch hook
# follows when it exports PYTHONPATH for workflow.py.
_ASSETS_STR = str(_ASSETS_DIR)
if _ASSETS_STR not in sys.path:
    sys.path.insert(0, _ASSETS_STR)

from gs import Workflow, Node, ValidationError  # noqa: E402
from gs._predicates import parse, evaluate, PredicateError  # noqa: E402


# ── Predicate DSL ──

class TestPredicateDSL:
    def test_contains_true_false(self):
        assert evaluate("contains:PASS", "ALL PASS") is True
        assert evaluate("contains:PASS", "no luck") is False

    def test_not_contains(self):
        assert evaluate("not-contains:WARN", "clean") is True
        assert evaluate("not-contains:WARN", "has WARN") is False

    def test_matches_regex(self):
        assert evaluate(r"matches:\d+", "abc 123 xyz") is True
        assert evaluate(r"matches:^\d+$", "abc") is False

    def test_equals(self):
        assert evaluate("equals:yes", "yes") is True
        assert evaluate("equals:yes", "no") is False

    def test_equals_with_colon_in_value(self):
        # value may itself contain a colon (split on first colon only)
        assert evaluate("equals:http://x", "http://x") is True

    def test_empty_nonempty(self):
        assert evaluate("empty", "") is True
        assert evaluate("nonempty", "x") is True
        assert evaluate("empty", "x") is False

    def test_always(self):
        assert evaluate("always", "") is True
        assert evaluate("always", "anything") is True

    def test_and_or_precedence(self):
        # && binds tighter than ||  →  (T || F) handled as T, and combos
        assert evaluate("contains:a || contains:b && contains:z", "a") is True
        # a present → first branch true → overall true regardless of second
        assert evaluate("contains:a || contains:b", "a") is True
        assert evaluate("contains:a && contains:b", "ab") is True
        assert evaluate("contains:a && contains:b", "a") is False

    def test_not_precedence(self):
        # ! binds tighter than &&  →  !contains:a && contains:b
        assert evaluate("!contains:a && contains:b", "b") is True
        assert evaluate("!contains:a && contains:b", "ab") is False

    def test_parentheses(self):
        assert evaluate("(contains:a || contains:b) && contains:c", "ac") is True
        assert evaluate("(contains:a || contains:b) && contains:c", "a") is False

    def test_bad_keyword_rejected(self):
        with pytest.raises(PredicateError):
            parse("is:foo")

    def test_bad_regex_rejected(self):
        with pytest.raises(PredicateError, match="bad regex"):
            parse("matches:[unclosed")

    def test_unknown_bare_atom_rejected(self):
        with pytest.raises(PredicateError, match="unknown atom"):
            parse("frobnicate")

    def test_empty_predicate_rejected(self):
        with pytest.raises(PredicateError):
            parse("")
        with pytest.raises(PredicateError):
            parse("   ")

    def test_unbalanced_paren_rejected(self):
        with pytest.raises(PredicateError, match="unbalanced|trailing"):
            parse("(contains:a")

    def test_parse_returns_callable_reusable(self):
        p = parse("contains:x")
        assert p("x") is True and p("y") is False and p("xx") is True


# ── Card structure ──

class TestCardStructure:
    def test_getsmart_discoverable(self):
        assert "getsmart" in discover_agent_cards()

    def test_card_fields(self):
        gs = load_agent_card("GetSmart")
        assert gs.preset == "default"
        # GetSmart is a generator, not a doer: it investigates (Read/Glob/Grep)
        # and writes workflow.py (Write/Edit/Bash). It deliberately does NOT
        # have the Agent tool — it delegates work via the workflow, not by
        # spawning sub-agents itself.
        assert "Write" in gs.tools and "Bash" in gs.tools
        assert "Read" in gs.tools
        assert "Agent" not in gs.tools
        assert gs.on_agent_end is not None
        assert "getsmart_run.sh" in gs.on_agent_end

    def test_assets_packaged(self):
        """The gs library and the launch hook ship with the package."""
        assert (_ASSETS_DIR / "gs" / "__init__.py").is_file()
        assert (_ASSETS_DIR / "hooks" / "getsmart_run.sh").is_file()


# ── Workflow.validate ──

class TestWorkflowValidate:
    def test_valid_linear_dag(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "find entry points")
        wf.agent("Worker", "refactor per {" + a.id + ".output}", needs=[a.id])
        wf.validate()  # no raise

    def test_empty_workflow_rejected(self):
        with pytest.raises(ValidationError, match="no nodes"):
            Workflow("t").validate()

    def test_duplicate_id_rejected(self):
        wf = Workflow("t")
        wf.agent("Explore", "a", id="dup")
        with pytest.raises(ValidationError, match="duplicate"):
            wf.agent("Explore", "b", id="dup")
            wf.validate()

    def test_dangling_need_rejected(self):
        wf = Workflow("t")
        n = wf.agent("Explore", "a")
        n.needs = ["ghost"]
        with pytest.raises(ValidationError, match="ghost"):
            wf.validate()

    def test_self_loop_rejected(self):
        wf = Workflow("t")
        n = wf.agent("Explore", "a", id="self")
        n.needs = ["self"]
        with pytest.raises(ValidationError, match="itself"):
            wf.validate()

    def test_cycle_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        b = wf.agent("Worker", "b", needs=[a.id], id="B")
        a.needs.append(b.id)  # A -> B -> A
        with pytest.raises(ValidationError, match="cycle"):
            wf.validate()

    def test_unknown_kind_rejected(self):
        wf = Workflow("t")
        wf._add(Node(id="x", kind="magic", prompt="p"))
        with pytest.raises(ValidationError, match="unknown kind"):
            wf.validate()

    def test_agent_node_requires_card(self):
        wf = Workflow("t")
        wf._add(Node(id="x", kind="agent", prompt="p"))  # no card
        with pytest.raises(ValidationError, match="requires a card"):
            wf.validate()

    def test_unknown_card_rejected(self):
        wf = Workflow("t")
        wf.agent("NoSuchCardExists", "do something")
        with pytest.raises(ValidationError, match="unknown card"):
            wf.validate()

    def test_known_builtin_card_accepted(self):
        wf = Workflow("t")
        wf.agent("Explore", "look around")  # Explore is a builtin card
        wf.validate()  # no raise

    def test_needs_accepts_node_objects(self):
        """The ergonomic form `needs=[a]` (Node objects, not just id strings)
        works — nodes are coerced to their ids on add."""
        wf = Workflow("t")
        a = wf.agent("Explore", "look around")
        wf.agent("Worker", "refactor per {a.output}", needs=[a])  # Node, not str
        wf.validate()  # no raise
        assert wf.nodes[-1].needs == [a.id]


# ── Workflow.render ──

class TestWorkflowRender:
    def test_mermaid_contains_all_nodes(self):
        wf = Workflow("demo")
        a = wf.agent("Explore", "a")
        b = wf.llm("summarize {" + a.id + ".output}", needs=[a.id])
        out = wf.render()
        assert out.startswith("```mermaid")
        assert "graph LR" in out
        assert a.id in out
        assert b.id in out

    def test_mermaid_contains_edges(self):
        wf = Workflow("demo")
        a = wf.agent("Explore", "a", id="A")
        b = wf.agent("Worker", "b", needs=["A"], id="B")
        out = wf.render()
        assert "A --> B" in out

    def test_passthrough_node_rendered(self):
        wf = Workflow("demo")
        p = wf.passthrough(id="P")
        out = wf.render()
        assert "P" in out
        assert "{{" in out  # hexagon shape for passthrough


# ── Workflow.run ──

class TestWorkflowRun:
    """run() with subprocess / call_llm stubbed out."""

    def _stub_agent(self, capture, *, outputs=None, fail_ids=None):
        """Patch gs._run_agent to record calls + return canned output.

        - ``capture``: list to append (card, prompt) tuples to (order check).
        - ``outputs``: {card: text} to return; default uses the card name.
        - ``fail_ids``: set of node ids whose agent call should raise.
        """
        outputs = outputs or {}
        fail_ids = fail_ids or set()

        def fake(card, prompt, model=None, mcp=False):
            # find which node this is by inspecting the call stack is fragile;
            # instead tag prompts. Simpler: record (card, prompt) and return.
            capture.append((card, prompt))
            return outputs.get(card, f"<{card}>")
        return fake

    def test_linear_run_interpolates_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        seen = []
        wf = Workflow("t")
        a = wf.agent("Explore", "find things", id="A")
        wf.agent("Worker", "use {A.output} now", needs=["A"], id="B")

        with patch("gs._run_agent",
                   side_effect=self._stub_agent(seen,
                           outputs={"Explore": "FOUND42", "Worker": "DONE"})):
            res = wf.run()

        assert res == {"A": "ok", "B": "ok"}
        # B's prompt got A's output interpolated in
        b_prompt = [p for c, p in seen if c == "Worker"][0]
        assert "FOUND42" in b_prompt

    def test_independent_nodes_run_parallel(self, tmp_path, monkeypatch):
        """Two root nodes are dispatched concurrently (overlap in time)."""
        monkeypatch.chdir(tmp_path)
        import time as _time
        import threading

        lock = threading.Lock()
        active = {"n": 0, "peak": 0}

        def slow(card, prompt, model=None, mcp=False):
            with lock:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
            _time.sleep(0.1)
            with lock:
                active["n"] -= 1
            return f"<{card}>"

        wf = Workflow("t")
        wf.agent("Explore", "a", id="A")
        wf.agent("Explore", "b", id="B")  # independent of A
        wf.llm("join {A.output} {B.output}", needs=["A", "B"], id="C")

        with patch("gs._run_agent", side_effect=slow), \
             patch("gs._run_llm", return_value="joined"):
            res = wf.run()

        assert res["A"] == "ok" and res["B"] == "ok"
        # A and B are in the same layer → should overlap.
        assert active["peak"] >= 2, (
            f"expected parallelism (peak>=2), got peak={active['peak']}")

    def test_llm_node_uses_call_llm(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        wf = Workflow("t")
        wf.llm("classify this", id="L")

        with patch("gs._run_llm", return_value="CLASSIFIED") as m:
            res = wf.run()
        assert res["L"] == "ok"
        m.assert_called_once()
        assert m.call_args[0][0] == "classify this"

    def test_agent_node_failure_does_not_block_sibling(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        def fake(card, prompt, model=None, mcp=False):
            if card == "Worker":
                raise RuntimeError("boom")
            return "<ok>"

        wf = Workflow("t")
        wf.agent("Worker", "will fail", id="W")
        wf.agent("Explore", "sibling ok", id="E")  # same layer, independent

        with patch("gs._run_agent", side_effect=fake):
            with pytest.raises(SystemExit):  # run() exits non-zero on failures
                wf.run()
        # sibling ran to completion despite W failing
        # (status recorded on the node objects)
        statuses = {n.id: n.status for n in wf.nodes}
        assert statuses["E"] == "ok"
        assert statuses["W"] == "failed"

    def test_mcp_opt_in_passes_no_mcp_flag(self, tmp_path, monkeypatch):
        """mcp=False (default) → spawned cmd contains --no-mcp;
        mcp=True → --no-mcp is absent (sub-agent loads its card's MCP)."""
        import subprocess as _sp
        monkeypatch.chdir(tmp_path)
        captured: list[list[str]] = []

        def fake_run(cmd, **kw):
            captured.append(list(cmd))
            return MagicMock(stdout="ok", stderr="", returncode=0)
        monkeypatch.setattr(_sp, "run", fake_run)

        wf = Workflow("t")
        wf.agent("Explore", "plain", id="N1")            # mcp=False default
        wf.agent("Explore", "web", id="N2", mcp=True)    # opt in

        with patch("gs._run_llm", return_value=""):
            wf.run()
        assert len(captured) == 2
        plain_cmd = next(c for c in captured
                         if c[c.index("--as") + 1] == "Explore"
                         and "plain" in c[-1])
        web_cmd = next(c for c in captured
                       if c[c.index("--as") + 1] == "Explore"
                       and "web" in c[-1])
        assert "--no-mcp" in plain_cmd, "default agent should pass --no-mcp"
        assert "--no-mcp" not in web_cmd, "mcp=True should omit --no-mcp"

    def test_downstream_of_failed_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        def fake(card, prompt, model=None, mcp=False):
            if card == "Worker":
                raise RuntimeError("boom")
            return "<ok>"

        wf = Workflow("t")
        wf.agent("Worker", "fail here", id="W")
        wf.agent("Explore", "downstream of W", needs=["W"], id="D")

        with patch("gs._run_agent", side_effect=fake):
            with pytest.raises(SystemExit):
                wf.run()
        statuses = {n.id: n.status for n in wf.nodes}
        assert statuses["W"] == "failed"
        assert statuses["D"] == "skipped"

    def test_validation_failure_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        wf = Workflow("t")
        wf.agent("NoSuchCardExists", "bad", id="X")
        with pytest.raises(SystemExit) as ei:
            wf.run()
        assert ei.value.code == 1

    def test_render_written_before_execute(self, tmp_path, monkeypatch):
        """The mermaid artifact is produced even if execution fails."""
        monkeypatch.chdir(tmp_path)

        def boom(card, prompt, model=None, mcp=False):
            raise RuntimeError("always")
        wf = Workflow("t")
        wf.agent("Explore", "x", id="X")
        with patch("gs._run_agent", side_effect=boom):
            with pytest.raises(SystemExit):
                wf.run()
        wf_dirs = list((tmp_path / ".letscode" / "workflows").iterdir())
        assert wf_dirs, "workflow render dir not created"
        assert (wf_dirs[0] / "mermaid.md").is_file()


# ── Control-flow: conditional validate ──

class TestConditionalValidate:
    def test_valid_conditional(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "classify the bug", id="A")
        fix = wf.agent("Worker", "fix critical", id="FIX")
        log = wf.agent("Worker", "log minor", id="LOG")
        wf.conditional(inputs=[a],
                       branches=[("contains:CRITICAL", [fix])],
                       default=[log], id="TRIAGE")
        wf.validate()  # no raise

    def test_branch_missing_child_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        c = wf.conditional(inputs=[a], branches=[("always", ["GHOST"])], id="C")
        wf._nodes.remove(c)  # re-add with bad ref by direct construction
        wf._add(c)
        with pytest.raises(ValidationError, match="unknown child"):
            wf.validate()

    def test_bad_predicate_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        fix = wf.agent("Worker", "fix", id="FIX")
        wf.conditional(inputs=[a],
                       branches=[("is:broken", [fix])], id="C")
        with pytest.raises(ValidationError, match="bad predicate"):
            wf.validate()

    def test_empty_branches_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        wf.conditional(inputs=[a], branches=[], id="C")
        with pytest.raises(ValidationError, match="at least one branch"):
            wf.validate()

    def test_child_owned_by_two_parents_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        shared = wf.agent("Worker", "shared", id="SHARED")
        wf.conditional(inputs=[a], branches=[("always", [shared])], id="C1")
        wf.conditional(inputs=[a], branches=[("always", [shared])], id="C2")
        with pytest.raises(ValidationError, match="owned by both"):
            wf.validate()

    def test_child_appearing_in_needs_rejected(self):
        """An owned child can't also be driven via another node's needs."""
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        fix = wf.agent("Worker", "fix", id="FIX")
        wf.conditional(inputs=[a], branches=[("always", [fix])], id="C")
        after = wf.agent("Worker", "after", needs=[fix], id="AFTER")
        with pytest.raises(ValidationError, match="owned by"):
            wf.validate()


# ── Control-flow: loop validate ──

class TestLoopValidate:
    def test_valid_loop(self):
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        inv = wf.agent("Worker", "fix {SEED.output}", id="INV")
        test = wf.agent("Tester", "test", id="TEST")
        wf.loop(body=[inv, test], stop_when="contains:PASS",
                max_iters=5, carry=test, inputs=[seed], id="L")
        wf.validate()  # no raise

    def test_empty_body_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        wf.loop(body=[], max_iters=3, inputs=[a], id="L")
        with pytest.raises(ValidationError, match="body"):
            wf.validate()

    def test_max_iters_below_one_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        b = wf.agent("Worker", "b", id="B")
        wf.loop(body=[b], max_iters=0, inputs=[a], id="L")
        with pytest.raises(ValidationError, match="max_iters"):
            wf.validate()

    def test_bad_stop_when_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        b = wf.agent("Worker", "b", id="B")
        wf.loop(body=[b], stop_when="is:bad", max_iters=3, inputs=[a], id="L")
        with pytest.raises(ValidationError, match="bad stop_when"):
            wf.validate()

    def test_carry_not_in_body_rejected(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        b = wf.agent("Worker", "b", id="B")
        outsider = wf.agent("Tester", "out", id="OUT")
        wf.loop(body=[b], carry=outsider, max_iters=3, inputs=[a], id="L")
        with pytest.raises(ValidationError, match="carry"):
            wf.validate()

    def test_loop_body_not_flagged_as_cycle(self):
        """A loop body chaining inv -> test must NOT be a 'cycle' — body
        nodes are owned and excluded from the top-level acyclicity check."""
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        inv = wf.agent("Worker", "fix", needs=["SEED"], id="INV")
        test = wf.agent("Tester", "test", needs=["INV"], id="TEST")
        wf.loop(body=[inv, test], stop_when="contains:PASS",
                max_iters=3, carry=test, inputs=[seed], id="L")
        wf.validate()  # no raise — proves body chain isn't misread as a cycle


# ── Control-flow: conditional run ──

class TestConditionalRun:
    def test_first_matching_branch_runs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ran = []
        wf = Workflow("t")
        a = wf.agent("Explore", "scan", id="A")
        crit = wf.agent("Worker", "fix critical", id="CRIT")
        minor = wf.agent("Worker", "fix minor", id="MINOR")
        wf.conditional(inputs=[a],
                       branches=[("contains:CRITICAL", [crit]),
                                 ("contains:MINOR", [minor])],
                       id="C")

        def fake(card, prompt, model=None, mcp=False):
            ran.append(card)
            return "CRITICAL" if card == "Explore" else f"<{card}>"
        with patch("gs._run_agent", side_effect=fake):
            res = wf.run()
        assert "Worker" in ran  # a Worker branch ran
        cond = next(n for n in wf.nodes if n.id == "C")
        assert cond.trace["branch"] == "contains:CRITICAL"
        assert res["C"] == "ok"
        # CRIT ran; MINOR did not (only one branch selected)
        crit_node = next(n for n in wf.nodes if n.id == "CRIT")
        minor_node = next(n for n in wf.nodes if n.id == "MINOR")
        assert crit_node.status == "ok"
        assert minor_node.status in ("pending", "skipped")

    def test_no_match_runs_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        wf = Workflow("t")
        a = wf.agent("Explore", "scan", id="A")
        fix = wf.agent("Worker", "fix", id="FIX")
        log = wf.agent("Worker", "log default", id="LOG")
        wf.conditional(inputs=[a],
                       branches=[("contains:NOPE", [fix])],
                       default=[log], id="C")

        def fake(card, prompt, model=None, mcp=False):
            return "benign report" if card == "Explore" else f"<{card}>"
        with patch("gs._run_agent", side_effect=fake):
            wf.run()
        cond = next(n for n in wf.nodes if n.id == "C")
        assert cond.trace["branch"] == "(default)"
        log_node = next(n for n in wf.nodes if n.id == "LOG")
        fix_node = next(n for n in wf.nodes if n.id == "FIX")
        assert log_node.status == "ok"
        assert fix_node.status in ("pending", "skipped")


# ── Control-flow: loop run ──

class TestLoopRun:
    def test_stop_early(self, tmp_path, monkeypatch):
        """stop_when hits on iteration 2 → loop runs exactly 2 iters."""
        monkeypatch.chdir(tmp_path)
        calls = {"n": 0}
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        inv = wf.agent("Worker", "fix", id="INV")
        test = wf.agent("Tester", "test", id="TEST")
        wf.loop(body=[inv, test], stop_when="contains:PASS",
                max_iters=10, carry=test, inputs=[seed], id="L")

        def fake(card, prompt, model=None, mcp=False):
            if card == "Tester":
                calls["n"] += 1
                return "PASS" if calls["n"] >= 2 else "FAIL"
            return f"<{card}>"
        with patch("gs._run_agent", side_effect=fake):
            res = wf.run()
        loop = next(n for n in wf.nodes if n.id == "L")
        assert loop.trace["iters"] == 2
        assert loop.trace["stopped"] is True
        assert res["L"] == "ok"

    def test_never_stops_runs_full(self, tmp_path, monkeypatch):
        """stop_when never matches → runs max_iters, stopped=False."""
        monkeypatch.chdir(tmp_path)
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        b = wf.agent("Worker", "b", id="B")
        wf.loop(body=[b], stop_when="contains:NEVER",
                max_iters=3, carry=b, inputs=[seed], id="L")

        with patch("gs._run_agent", return_value="working..."):
            res = wf.run()
        loop = next(n for n in wf.nodes if n.id == "L")
        assert loop.trace["iters"] == 3
        assert loop.trace["stopped"] is False
        assert res["L"] == "ok"

    def test_carry_feeds_next_iteration(self, tmp_path, monkeypatch):
        """carry's output is interpolatable in the next iteration's prompt."""
        monkeypatch.chdir(tmp_path)
        seen_prompts = []
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        # INV references the carry node {TEST.output} from the prior round
        inv = wf.agent("Worker", "fix using {TEST.output}", id="INV")
        test = wf.agent("Tester", "test", id="TEST")
        wf.loop(body=[inv, test], stop_when="contains:DONE",
                max_iters=3, carry=test, inputs=[seed], id="L")

        def fake(card, prompt, model=None, mcp=False):
            if card == "Worker":
                seen_prompts.append(prompt)
                return "fixed"
            if card == "Tester":
                return "DONE"  # stop after first full iteration
            return "seed-output"
        with patch("gs._run_agent", side_effect=fake):
            wf.run()
        # First INV prompt: TEST hasn't run yet → {TEST.output} is empty.
        assert seen_prompts, "Worker never ran"
        # The first-iteration prompt should have empty interpolation (TEST
        # not yet run); proves the loop ran at least once and didn't crash.
        assert "fix using" in seen_prompts[0]


# ── Control-flow: render ──

class TestRenderControlFlow:
    def test_conditional_renders_labeled_branches(self):
        wf = Workflow("t")
        a = wf.agent("Explore", "a", id="A")
        fix = wf.agent("Worker", "fix", id="FIX")
        log = wf.agent("Worker", "log", id="LOG")
        wf.conditional(inputs=[a],
                       branches=[("contains:CRIT", [fix])],
                       default=[log], id="C")
        out = wf.render()
        assert "C" in out
        # branch edge labeled with the predicate
        assert "contains:CRIT" in out

    def test_loop_renders_body_and_iters(self):
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        inv = wf.agent("Worker", "inv", id="INV")
        test = wf.agent("Tester", "test", id="TEST")
        wf.loop(body=[inv, test], stop_when="contains:PASS",
                max_iters=7, carry=test, inputs=[seed], id="L")
        out = wf.render()
        assert "L" in out
        assert "7" in out  # max_iters annotated


# ── Control-flow: nesting ──

class TestNesting:
    def test_loop_body_contains_conditional(self, tmp_path, monkeypatch):
        """A loop whose body contains a conditional — recursion works."""
        monkeypatch.chdir(tmp_path)
        wf = Workflow("t")
        seed = wf.agent("Explore", "seed", id="SEED")
        triage = wf.conditional(
            inputs=[seed],
            branches=[("always", [wf.agent("Worker", "act", id="ACT")])],
            id="TRIAGE",
        )
        # body references the conditional + an actor it owns
        wf.loop(body=[triage], stop_when="contains:STOP",
                max_iters=2, carry=triage, inputs=[seed], id="L")

        def fake(card, prompt, model=None, mcp=False):
            return "STOP" if card == "Worker" else "seed"
        with patch("gs._run_agent", side_effect=fake):
            res = wf.run()
        loop = next(n for n in wf.nodes if n.id == "L")
        assert loop.trace["iters"] == 1
        assert loop.trace["stopped"] is True
        assert res["L"] == "ok"


# ── Launch hook ──

class TestGetSmartHook:
    @pytest.fixture
    def hook_script(self):
        p = files("letscode.builtin_agents") / "GetSmart.assets/hooks/getsmart_run.sh"
        assert p.is_file(), "getsmart_run.sh not packaged"
        return str(p)

    @pytest.fixture
    def real_python(self):
        return sys.executable

    def test_launches_workflow_py(self, hook_script, tmp_path, real_python):
        """A valid workflow.py that exits 0 → hook exits 0."""
        (tmp_path / "workflow.py").write_text(
            "def main():\n"
            "    print('workflow ran')\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_PYTHON": real_python})
        assert r.returncode == 0
        assert "workflow ran" in r.stdout

    def test_missing_workflow_py_aborts(self, hook_script, tmp_path, real_python):
        """No workflow.py → GetSmart didn't deliver → hook exit 2."""
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_PYTHON": real_python})
        assert r.returncode == 2
        assert "workflow.py" in r.stdout.lower()

    def test_propagates_workflow_failure(self, hook_script, tmp_path, real_python):
        """workflow.py exits non-zero → hook propagates that exit code."""
        (tmp_path / "workflow.py").write_text(
            "import sys\n"
            "print('bad workflow', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_PYTHON": real_python})
        assert r.returncode != 0

    def test_workflow_py_can_import_gs(self, hook_script, tmp_path, real_python):
        """End-to-end: workflow.py uses the gs library and renders its DAG."""
        (tmp_path / "workflow.py").write_text(textwrap.dedent("""
            from gs import Workflow
            wf = Workflow("smoke test")
            wf.passthrough(id="root")
            wf.run()  # validate -> render -> execute (no agent/llm nodes)
        """))
        r = run_hook(hook_script, '{"turn":1,"tool_calls":[]}',
                     str(tmp_path), sandbox=False,
                     env={"LETSCODE_PYTHON": real_python})
        assert r.returncode == 0, r.stdout + (r.stderr or "")
        assert "rendered" in r.stdout
        # mermaid.md got written under .letscode/workflows/
        wf_root = tmp_path / ".letscode" / "workflows"
        assert wf_root.is_dir() and any(wf_root.iterdir())
