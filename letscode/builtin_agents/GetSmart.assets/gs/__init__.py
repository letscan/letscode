"""gs — the workflow library GetSmart-generated ``workflow.py`` programs use.

This is **GetSmart's own asset**, not part of letscode core. letscode provides
agent-core + hooks; GetSmart ships this library under ``GetSmart.assets/gs/``
so that the ``workflow.py`` a GetSmart run produces is a self-contained,
re-runnable Python program.

The model:

    from gs import Workflow

    wf = Workflow("the user's original task")
    a = wf.agent("Explore", "list all entry points of the auth module")
    b = wf.agent("Worker", "refactor auth.py per {a.output}", needs=[a])
    wf.llm("summarize {b.output} in Chinese", needs=[b])

    if __name__ == "__main__":
        wf.run()   # validate -> render(mermaid) -> topological execute

Design lineage:
- **Generated-and-executed program** (Voyager / AutoGen): the workflow IS a
  Python program the LLM emits, not a static plan.
- **Deterministic validate before execute** (DevHard's lesson): the run never
  trusts self-report; ``run()`` validates the DAG is well-formed (acyclic,
  every ``needs`` resolved, every agent card exists) and aborts with a clear
  error if not. "GetSmart's acceptance" = the generated workflow artifact
  itself is correct & usable.
- **Two workflows, separated**: GetSmart-the-agent is orchestrated by a single
  minimal ``onAgentEnd`` hook (just launches ``workflow.py``); the workflow
  ``workflow.py`` describes is driven by Python, not by hooks.

letscode core is imported lazily (inside executors) so this module imports
cleanly even in environments where letscode isn't on the path — the pure DAG
machinery (build/validate/render) has no letscode dependency.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

__all__ = ["Workflow", "Node", "ValidationError"]


# How many agent/llm nodes may run at once within one topological "layer".
# Default 2: in practice, higher concurrency (e.g. 4) against a single shared
# model/API causes intermittent mid-stream sub-agent interruptions (one of N
# parallel streams gets cut → empty stdout → spurious failure). 2 is the
# empirically safe default; callers can raise it via run(max_workers=...) when
# the backing API is known to tolerate the load.
MAX_WORKERS = 2

# All valid node kinds: leaves (do work) + control-flow (drive children).
_LEAF_KINDS = ("agent", "llm", "passthrough")
_CONTROL_KINDS = ("conditional", "loop")
_ALL_KINDS = _LEAF_KINDS + _CONTROL_KINDS

# Regex for ``{node_id.output}`` interpolation in prompts. Matches dotted refs
# whose head is a node id; leaves anything else (e.g. dict-literal examples in
# a prompt) untouched.
_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\.output\}")


class ValidationError(Exception):
    """Raised when a workflow DAG is malformed (cycle / dangling ref / ...)."""


@dataclass
class Node:
    """One node in the workflow DAG.

    Leaf kinds (do work):
    - ``kind="agent"``: spawns ``letscode --as <card>``; ``card`` required.
    - ``kind="llm"``: a single ``call_llm`` step; ``model`` optional.
    - ``kind="passthrough"``: no-op placeholder (branch merge / anchor).

    Control-flow kinds (drive their own child nodes, referenced by id in
    ``branches`` / ``default_branch`` / ``body``; children are *owned* by this
    node and excluded from the top-level layering):
    - ``kind="conditional"``: evaluates ``branches`` predicates against the
      joined ``inputs`` outputs, runs the first matching branch's children.
      ``default_branch`` runs if none match.
    - ``kind="loop"``: runs ``body`` up to ``max_iters`` times, stopping early
      when ``stop_when`` matches ``carry``'s output.

    Predicate strings use the restricted DSL in :mod:`gs._predicates`
    (``contains:`` / ``matches:`` / ``equals:`` / ``empty`` / ``always`` +
    ``&&``/``||``/``!``/parens) — no arbitrary code, so control-flow stays
    statically analyzable.
    """

    id: str
    kind: str
    prompt: str = ""
    card: str | None = None
    model: str | None = None
    needs: list[str] = field(default_factory=list)
    # ── per-node execution options ──
    # mcp: if True, the spawned sub-agent loads MCP (the card's mcp_servers
    # whitelist scopes which servers). Default False (--no-mcp) matches the
    # Agent-tool convention; set True for cards that need web/external tools.
    mcp: bool = False
    # ── control-flow fields (conditional/loop only) ──
    # conditional: list of (predicate_dsl, [child_ids])
    branches: list[tuple[str, list[str]]] = field(default_factory=list)
    # conditional: child ids run when no branch predicate matches
    default_branch: list[str] = field(default_factory=list)
    # conditional: node ids whose outputs are joined to test predicates
    inputs: list[str] = field(default_factory=list)
    # loop: child ids run each iteration (topologically)
    body: list[str] = field(default_factory=list)
    # loop: stop predicate DSL (default "always" → runs exactly max_iters)
    stop_when: str = "always"
    # loop: iteration cap (≥1)
    max_iters: int = 1
    # loop: which body node's output feeds the next iteration's first node
    # (via {carry.output}); None → no carry-through
    carry: str | None = None
    # ── execution state ──
    output: str = ""
    status: str = "pending"  # pending | running | ok | failed | skipped
    error: str = ""
    # post-run record of which path control flow actually took
    # (conditional: {"branch": dsl|None}; loop: {"iters": n, "stopped": bool})
    trace: dict = field(default_factory=dict)

    def ref(self) -> str:
        """The ``{id.output}`` token usable in downstream prompts."""
        return "{" + f"{self.id}.output" + "}"


class Workflow:
    """A buildable, validatable, renderable, executable DAG.

    Construction is side-effect free (no I/O, no letscode import). Only
    :meth:`run` touches the filesystem / spawns processes / calls models.
    """

    def __init__(self, task: str = "") -> None:
        self.task = task
        self._nodes: list[Node] = []
        self._counter = 0

    # ── DAG construction ────────────────────────────────────────────────

    def _add(self, node: Node) -> Node:
        # Normalize needs[]: accept Node objects (ergonomic: needs=[a]) or
        # bare id strings (needs=["A"]); store the id strings the rest of the
        # code compares against. Drop any None entries silently.
        node.needs = [_dep_id(d) for d in node.needs if d is not None]
        self._nodes.append(node)
        return node

    def _auto_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter}"

    def agent(
        self,
        card: str,
        prompt: str,
        *,
        needs: list[str] | None = None,
        id: str | None = None,
        mcp: bool = False,
    ) -> Node:
        """Add a node that spawns a letscode sub-agent (``--as <card>``).

        ``mcp=True`` lets the sub-agent load MCP servers (needed for cards
        like Research that declare ``mcp_servers``); the card's own whitelist
        still scopes which servers load. Default ``False`` (``--no-mcp``).
        """
        return self._add(Node(
            id=id or self._auto_id(_id_from_card(card)),
            kind="agent", prompt=prompt, card=card,
            needs=list(needs or []), mcp=mcp,
        ))

    def llm(
        self,
        prompt: str,
        *,
        needs: list[str] | None = None,
        model: str | None = None,
        id: str | None = None,
    ) -> Node:
        """Add a node that runs a single lightweight ``call_llm`` step."""
        return self._add(Node(
            id=id or self._auto_id("llm"),
            kind="llm", prompt=prompt, model=model, needs=list(needs or []),
        ))

    def passthrough(self, *, id: str | None = None) -> Node:
        """Add a no-op node (anchor / merge point; consumes no tokens)."""
        return self._add(Node(id=id or self._auto_id("pass"), kind="passthrough"))

    def conditional(
        self,
        *,
        inputs,
        branches: list[tuple[str, list]],
        default: list | None = None,
        needs: list | None = None,
        id: str | None = None,
    ) -> Node:
        """Add a conditional (3rd primitive): branch on a predecessor's output.

            explore = wf.agent("Explore", "classify the bug")
            wf.conditional(
                inputs=[explore],
                branches=[
                    ("contains:CRITICAL", [fix_critical]),
                    ("matches:MINOR|TRIVIAL", [fix_minor]),
                ],
                default=[log_and_skip],
            )

        ``inputs`` / branch-child entries accept Node objects or id strings.
        At run time, the ``inputs`` nodes' outputs are joined with newlines and
        each branch's predicate (restricted DSL) is tested in order; the first
        match's children run. ``default`` runs if none match. Children are
        owned by this node (not driven elsewhere).
        """
        return self._add(Node(
            id=id or self._auto_id("cond"),
            kind="conditional",
            needs=list(needs or []),
            inputs=[_dep_id(d) for d in (inputs or [])],
            branches=[(dsl, [_dep_id(c) for c in kids])
                      for dsl, kids in (branches or [])],
            default_branch=[_dep_id(c) for c in (default or [])],
        ))

    def loop(
        self,
        *,
        body: list,
        stop_when: str = "always",
        max_iters: int = 1,
        carry=None,
        inputs: list | None = None,
        needs: list | None = None,
        id: str | None = None,
    ) -> Node:
        """Add a loop (4th primitive): run ``body`` until ``stop_when`` or cap.

            seed = wf.agent("Explore", "find the failing case")
            inv = wf.agent("Worker", "investigate one fix from {seed.output}")
            test = wf.agent("Tester", "run tests")
            wf.loop(
                body=[inv, test],
                stop_when="contains:ALL_PASS",
                max_iters=10,
                carry=test,            # test's output feeds next round's inv
                inputs=[seed],
            )

        ``body`` / ``carry`` / ``inputs`` accept Node objects or id strings.
        Each iteration runs ``body`` topologically; after it, ``stop_when`` is
        tested against ``carry``'s output (or the last body node) — a match
        stops the loop early. ``max_iters`` is the hard cap (≥1). The carry
        node's output is interpolatable in the next round's prompts as
        ``{carry_id.output}``.
        """
        return self._add(Node(
            id=id or self._auto_id("loop"),
            kind="loop",
            needs=list(needs or []),
            inputs=[_dep_id(d) for d in (inputs or [])],
            body=[_dep_id(c) for c in (body or [])],
            stop_when=stop_when,
            max_iters=max_iters,
            carry=_dep_id(carry) if carry is not None else None,
        ))

    @property
    def nodes(self) -> list[Node]:
        return list(self._nodes)

    # ── validate ────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Check the DAG is well-formed. Raises :class:`ValidationError`.

        This is GetSmart's acceptance: the generated workflow artifact itself
        must be correct & usable. The runner never trusts self-report.
        """
        if not self._nodes:
            raise ValidationError("workflow has no nodes")

        ids: set[str] = set()
        for n in self._nodes:
            if not n.id:
                raise ValidationError("every node needs an id")
            if n.id in ids:
                raise ValidationError(f"duplicate node id: {n.id!r}")
            ids.add(n.id)

            if n.kind not in _ALL_KINDS:
                raise ValidationError(
                    f"node {n.id!r}: unknown kind {n.kind!r} "
                    f"(expected one of {sorted(_ALL_KINDS)})"
                )
            if n.kind == "agent" and not n.card:
                raise ValidationError(f"node {n.id!r}: agent node requires a card")
            if n.kind in ("agent", "llm") and not n.prompt:
                raise ValidationError(
                    f"node {n.id!r}: {n.kind} node requires a prompt"
                )

        # Compute child ownership: which nodes are owned (driven internally) by
        # a conditional/loop. Owned nodes are excluded from the top-level
        # acyclicity check and top-level layering — they're driven by their
        # parent's executor instead. A node may be owned by at most ONE parent.
        self._owned: dict[str, str] = {}  # child_id -> owning parent_id
        for n in self._nodes:
            if n.kind in ("conditional", "loop"):
                kids = self._children_of(n)
                for k in kids:
                    if k == n.id:
                        raise ValidationError(
                            f"{n.kind} node {n.id!r} lists itself as a child"
                        )
                    if k not in ids:
                        raise ValidationError(
                            f"{n.kind} node {n.id!r}: references unknown "
                            f"child node {k!r}"
                        )
                    if k in self._owned:
                        raise ValidationError(
                            f"node {k!r} is owned by both {self._owned[k]!r} "
                            f"and {n.id!r} — a child can belong to only one "
                            "conditional/loop"
                        )
                    self._owned[k] = n.id

        # Control-flow node field validation (after ownership is known).
        for n in self._nodes:
            if n.kind == "conditional":
                self._validate_conditional(n, ids)
            elif n.kind == "loop":
                self._validate_loop(n, ids)

        # Every needs[] must point at an existing node. Owned nodes are driven
        # internally by their parent — but a child may legitimately need a
        # SIBLING under the same parent (that's how subgraph ordering works in
        # _run_subgraph). The forbidden case is cross-ownership: a node
        # depending on another parent's child, or a top-level node depending
        # on an owned child.
        for n in self._nodes:
            n_owner = self._owned.get(n.id)  # None if n is top-level
            for dep in n.needs:
                if dep not in ids:
                    raise ValidationError(
                        f"node {n.id!r}: needs unknown node {dep!r}"
                    )
                if dep == n.id:
                    raise ValidationError(f"node {n.id!r}: depends on itself")
                dep_owner = self._owned.get(dep)
                # dep is owned by a different parent than n → cross-ownership.
                if dep_owner is not None and dep_owner != n_owner:
                    raise ValidationError(
                        f"node {n.id!r}: needs {dep!r}, but {dep!r} is owned "
                        f"by {dep_owner!r} (only its parent or a sibling may "
                        "drive it)"
                    )

        self._check_acyclic()

        # Agent cards must be discoverable by letscode. Done last so purely
        # structural errors surface first (cheaper to diagnose). Lazily import
        # letscode — validate() still works for DAG-shape checks in envs
        # without letscode, but card-existence requires it.
        missing = self._missing_cards()
        if missing:
            raise ValidationError(
                "agent node(s) reference unknown card(s): "
                + ", ".join(sorted(missing))
            )

    def _check_acyclic(self) -> None:
        # Only consider TOP-LEVEL nodes: a conditional/loop's owned children
        # are driven internally by their parent, not by the top-level needs
        # graph. Including them would flag a legitimate loop body (which
        # chains body[i] -> body[i+1]) as a cycle.
        owned = getattr(self, "_owned", {})
        by_id = {n.id: n for n in self._nodes if n.id not in owned}
        # Kahn's algorithm; a leftover set after peeling means a cycle.
        indeg = {nid: 0 for nid in by_id}
        for n in by_id.values():
            for dep in n.needs:
                if dep in by_id:  # ignore deps that are owned children
                    indeg[n.id] += 1  # edge dep -> n
        # successors[dep] = [nodes that need dep]
        succ: dict[str, list[str]] = {nid: [] for nid in by_id}
        for n in by_id.values():
            for dep in n.needs:
                if dep in by_id:
                    succ[dep].append(n.id)
        queue = [nid for nid, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for nxt in succ[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if seen != len(by_id):
            remaining = sorted(nid for nid, d in indeg.items() if d > 0)
            raise ValidationError(
                "workflow has a cycle involving: " + ", ".join(remaining)
            )

    @staticmethod
    def _children_of(n: Node) -> list[str]:
        """All child ids referenced by a control-flow node (branches/default/body)."""
        kids: list[str] = []
        if n.kind == "conditional":
            for _dsl, cids in n.branches:
                kids.extend(cids)
            kids.extend(n.default_branch)
        elif n.kind == "loop":
            kids.extend(n.body)
        return kids

    def _validate_conditional(self, n: Node, ids: set[str]) -> None:
        from ._predicates import parse, PredicateError

        if not n.branches:
            raise ValidationError(
                f"conditional node {n.id!r}: needs at least one branch"
            )
        for dsl, cids in n.branches:
            if not cids:
                raise ValidationError(
                    f"conditional node {n.id!r}: branch {dsl!r} has no children"
                )
            try:
                parse(dsl)
            except PredicateError as e:
                raise ValidationError(
                    f"conditional node {n.id!r}: bad predicate {dsl!r}: {e}"
                ) from e
            for c in cids:
                if c not in ids:
                    raise ValidationError(
                        f"conditional node {n.id!r}: branch {dsl!r} references "
                        f"unknown node {c!r}"
                    )
        for c in n.default_branch:
            if c not in ids:
                raise ValidationError(
                    f"conditional node {n.id!r}: default references "
                    f"unknown node {c!r}"
                )

    def _validate_loop(self, n: Node, ids: set[str]) -> None:
        from ._predicates import parse, PredicateError

        if not n.body:
            raise ValidationError(
                f"loop node {n.id!r}: body must list at least one node"
            )
        if n.max_iters < 1:
            raise ValidationError(
                f"loop node {n.id!r}: max_iters must be ≥ 1 (got {n.max_iters})"
            )
        try:
            parse(n.stop_when)
        except PredicateError as e:
            raise ValidationError(
                f"loop node {n.id!r}: bad stop_when {n.stop_when!r}: {e}"
            ) from e
        for c in n.body:
            if c not in ids:
                raise ValidationError(
                    f"loop node {n.id!r}: body references unknown node {c!r}"
                )
        if n.carry is not None and n.carry not in n.body:
            raise ValidationError(
                f"loop node {n.id!r}: carry {n.carry!r} must be a body member"
            )

    def _missing_cards(self) -> set[str]:
        agent_cards = {n.card for n in self._nodes if n.kind == "agent" and n.card}
        if not agent_cards:
            return set()
        try:
            from letscode.agent_card import discover_agent_cards
        except Exception:
            # letscode not importable in this env — can't verify; skip rather
            # than false-positive. Real runs always have letscode installed.
            return set()
        known = set(discover_agent_cards().keys())
        return {c for c in agent_cards if c.lower() not in known}

    # ── render ──────────────────────────────────────────────────────────

    def render(self) -> str:
        """Return a Mermaid ``graph LR`` rendering of the DAG.

        Pure string build; no I/O. Callers (``run()``) persist it to disk so
        the generated workflow is inspectable even if execution later fails.

        Control-flow nodes render their parent→child relationships as labeled
        edges: conditional branches carry their predicate (and ``else`` for the
        default), loops annotate ``max_iters`` on a repeat edge. This shows the
        FULL static plan — every branch / loop body is visible before any run.
        """
        owned = getattr(self, "_owned", {})
        by_id = {n.id: n for n in self._nodes}
        lines = ["```mermaid", "graph LR"]
        for n in self._nodes:
            shape = _mermaid_shape(n)
            label = _mermaid_label(n)
            lines.append(f'    {n.id}{shape[0]}"{label}"{shape[1]}')

        seen_edge: set[tuple[str, str, str]] = set()

        def edge(src: str, dst: str, label: str = "") -> None:
            key = (src, dst, label)
            if key in seen_edge:
                return
            seen_edge.add(key)
            if label:
                # Mermaid edge label syntax: A -->|text| B
                lines.append(f"    {src} -->|{label}| {dst}")
            else:
                lines.append(f"    {src} --> {dst}")

        # 1) needs-based edges (top-level + sibling-within-subgraph).
        for n in self._nodes:
            for dep in n.needs:
                edge(dep, n.id)

        # 2) control-flow parent → child edges (with labels / annotations).
        for n in self._nodes:
            if n.kind == "conditional":
                for dsl, cids in n.branches:
                    if cids:
                        edge(n.id, cids[0], dsl)
                    # chain branch siblings via their needs (already drawn),
                    # but if a branch child has no needs, link sequentially.
                    for a, b in zip(cids, cids[1:]):
                        if b not in by_id or not by_id[b].needs:
                            edge(a, b)
                for c in n.default_branch:
                    edge(n.id, c, "else")
            elif n.kind == "loop":
                if n.body:
                    edge(n.id, n.body[0])
                    # sequential body links where not already expressed via needs
                    for a, b in zip(n.body, n.body[1:]):
                        if b not in by_id or not by_id[b].needs:
                            edge(a, b)
                    # repeat edge: last body → first, annotated with the cap
                    edge(n.body[-1], n.body[0], f"repeat×{n.max_iters}")

        if not any(n.needs for n in self._nodes) and not any(
            n.kind in _CONTROL_KINDS for n in self._nodes
        ):
            lines.append("    %% (no dependencies — all nodes are roots)")
        lines.append("```")
        return "\n".join(lines)

    # ── topological layers ──────────────────────────────────────────────

    def _layers(self) -> list[list[Node]]:
        """Group TOP-LEVEL nodes into topological layers for parallel execution.

        Layer 0 = roots (no needs). Layer k = nodes whose needs are all in
        layers < k. Nodes within a layer are independent → run concurrently.

        Owned children (driven by a conditional/loop parent) are excluded —
        they're run by :meth:`_run_subgraph` inside their parent's executor,
        not by the top-level sweep. Requires :meth:`validate` has passed.
        """
        owned = getattr(self, "_owned", {})
        top = [n for n in self._nodes if n.id not in owned]
        placed: dict[str, int] = {}
        layers: list[list[Node]] = []
        remaining = list(top)
        while remaining:
            layer: list[Node] = []
            for n in remaining:
                if all((d in placed or d in owned) for d in n.needs):
                    layer.append(n)
            if not layer:
                # Should be unreachable post-validate; guard anyway.
                raise ValidationError("cycle detected during layering")
            for n in layer:
                placed[n.id] = len(layers)
                remaining.remove(n)
            layers.append(layer)
        return layers

    # ── execute ─────────────────────────────────────────────────────────

    def run(self, *, max_workers: int = MAX_WORKERS) -> dict[str, str]:
        """Validate, render (persist), then execute the DAG topologically.

        Returns ``{node_id: status}``. Exits non-zero (``sys.exit(1)``) on
        validation failure — a malformed generated workflow is GetSmart's
        failure to deliver, and is reported honestly rather than papered over.

        Per-node execution failures do NOT abort the whole run: sibling nodes
        in the same layer still run, and downstream nodes of a failed node are
        skipped with status ``"skipped"``. The process exit code is non-zero
        if any node failed, so the launching hook can surface the outcome.
        """
        try:
            self.validate()
        except ValidationError as e:
            print(f"workflow validation failed: {e}", file=sys.stderr)
            sys.exit(1)

        # Persist the rendered DAG FIRST — it's the showcase artifact, and
        # must survive even if execution blows up halfway.
        out_dir = self._write_render()
        print(f"[gs] workflow DAG rendered → {out_dir}/mermaid.md")

        results: dict[str, str] = {n.id: n.status for n in self._nodes}
        outputs: dict[str, str] = {n.id: n.output for n in self._nodes}
        failed: set[str] = set()

        for layer in self._layers():
            runnable = [n for n in layer
                        if not any(d in failed for d in n.needs)]
            skipped = [n for n in layer if n not in runnable]
            for n in skipped:
                n.status = "skipped"
                results[n.id] = "skipped"
                print(f"[gs] skip {n.id} (a dependency failed)")

            if not runnable:
                continue

            with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
                future_to_node = {
                    pool.submit(self._exec_node, n, outputs): n for n in runnable
                }
                for fut in as_completed(future_to_node):
                    n = future_to_node[fut]
                    try:
                        out = fut.result()
                    except Exception as e:  # executor blew up unexpectedly
                        n.status = "failed"
                        n.error = str(e)
                        out = ""
                    else:
                        n.status = "ok"
                    n.output = out
                    outputs[n.id] = out
                    results[n.id] = n.status
                    if n.status != "ok":
                        failed.add(n.id)
                    tag = "ok" if n.status == "ok" else f"FAIL: {n.error}"
                    print(f"[gs] {n.status:>8} {n.id} ({n.kind}) — {tag}")

        self._write_run_log(out_dir, results)
        self._write_outputs(out_dir)
        bad = [nid for nid, s in results.items() if s in ("failed", "skipped")]
        if bad:
            print(f"\n[gs] workflow finished with {len(bad)} non-ok node(s): "
                  + ", ".join(bad), file=sys.stderr)
            sys.exit(1)
        print(f"\n[gs] workflow finished: all {len(results)} node(s) ok.")
        # Surface the final deliverable to the terminal: the output of the
        # last-executed top-level node (typically a synthesize/summary node).
        # It's also persisted to <out_dir>/outputs/<id>.md for the record.
        top_ids = [n.id for n in self._nodes if n.id not in getattr(self, "_owned", {})]
        deliverable = next((nid for nid in reversed(top_ids)
                            if outputs.get(nid)), None)
        if deliverable:
            print(f"\n[gs] deliverable ({deliverable}) → "
                  f"{out_dir}/outputs/{deliverable}.md")
            print("\n" + outputs[deliverable])
        return results

    def _exec_node(self, node: Node, outputs: dict[str, str]) -> str:
        """Run one node, returning its output text. Sets node.error on raise.

        For control-flow nodes (conditional/loop), returns the selected
        branch's / final iteration's tail output and records a ``trace`` dict
        on the node describing which path was actually taken.
        """
        node.status = "running"
        if node.kind == "passthrough":
            return ""
        if node.kind == "conditional":
            return self._exec_conditional(node, outputs)
        if node.kind == "loop":
            return self._exec_loop(node, outputs)
        prompt = self._interpolate(node.prompt, outputs)
        if node.kind == "agent":
            assert node.card is not None
            return _run_agent(node.card, prompt, node.model, node.mcp)
        if node.kind == "llm":
            return _run_llm(prompt, node.model)
        return ""

    def _exec_conditional(self, node: Node, outputs: dict[str, str]) -> str:
        """Evaluate predicates against inputs' outputs; run the first match."""
        from ._predicates import evaluate

        judged = "\n".join(outputs.get(i, "") for i in node.inputs)
        chosen: tuple[str, list[str]] | None = None
        for dsl, cids in node.branches:
            if evaluate(dsl, judged):
                chosen = (dsl, cids)
                break
        if chosen is None:
            if node.default_branch:
                chosen = ("(default)", list(node.default_branch))
            else:
                # No branch matched and no default — a no-op conditional.
                node.trace = {"branch": None, "reason": "no match, no default"}
                print(f"[gs]     {node.id}: no branch matched, no default")
                return ""
        dsl, cids = chosen
        print(f"[gs]     {node.id}: branch {dsl!r} selected")
        node.trace = {"branch": dsl}
        tail = self._run_subgraph(cids, outputs, owner=node.id)
        return tail

    def _exec_loop(self, node: Node, outputs: dict[str, str]) -> str:
        """Run body up to max_iters, stopping when stop_when matches carry."""
        from ._predicates import evaluate

        # Seed: inputs' outputs feed the first iteration's interpolation via
        # {inputs_id.output}; nothing extra needed — outputs already has them.
        tail = ""
        iters = 0
        stopped = False
        for it in range(node.max_iters):
            iters = it + 1
            print(f"[gs]     {node.id}: iteration {iters}/{node.max_iters}")
            tail = self._run_subgraph(list(node.body), outputs, owner=node.id)
            # Decide stop condition against carry (or body's last node).
            probe_id = node.carry or node.body[-1]
            probe_out = outputs.get(probe_id, tail)
            if evaluate(node.stop_when, probe_out):
                stopped = True
                break
        node.trace = {"iters": iters, "stopped": stopped,
                      "max_iters": node.max_iters}
        return tail

    def _run_subgraph(self, node_ids: list[str], outputs: dict[str, str],
                      *, owner: str, max_workers: int = MAX_WORKERS) -> str:
        """Topologically run a set of owned child nodes, updating ``outputs``.

        Used by conditional branches and loop bodies. Children may themselves
        be control-flow nodes → recursion (e.g. a loop whose body contains a
        conditional). Returns the last node's output (best-effort tail).
        """
        by_id = {n.id: n for n in self._nodes if n.id in set(node_ids)}
        if not by_id:
            return ""
        # Topo layers over the subgraph, using ONLY internal needs (a child's
        # needs that point outside the subgraph — e.g. the loop's seed input —
        # are already satisfied in ``outputs`` and ignored for ordering).
        ids = set(by_id)
        placed: set[str] = set()
        layers: list[list[Node]] = []
        remaining = list(by_id.values())
        while remaining:
            layer = [n for n in remaining
                     if all((d in placed or d not in ids) for d in n.needs)]
            if not layer:
                raise ValidationError(
                    f"cycle in subgraph owned by {owner!r}: "
                    + ", ".join(n.id for n in remaining)
                )
            for n in layer:
                placed.add(n.id)
                remaining.remove(n)
            layers.append(layer)

        tail = ""
        failed: set[str] = set()
        for layer in layers:
            runnable = [n for n in layer
                        if not any((d in failed for d in n.needs
                                    if d in ids))]
            for n in layer:
                if n not in runnable:
                    n.status = "skipped"
                    outputs[n.id] = ""
            if not runnable:
                continue
            with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
                futs = {pool.submit(self._exec_node, n, outputs): n
                        for n in runnable}
                for fut in as_completed(futs):
                    n = futs[fut]
                    try:
                        out = fut.result()
                        n.status = "ok"
                    except Exception as e:
                        n.status = "failed"
                        n.error = str(e)
                        out = ""
                        failed.add(n.id)
                    n.output = out
                    outputs[n.id] = out
                    tail = out or tail
                    tag = "ok" if n.status == "ok" else f"FAIL: {n.error}"
                    print(f"[gs]   {n.status:>8} {n.id} ({n.kind}) — {tag}")
        return tail

    @staticmethod
    def _interpolate(prompt: str, outputs: dict[str, str]) -> str:
        """Replace ``{node_id.output}`` with that node's captured output."""
        def repl(m: re.Match) -> str:
            nid = m.group(1)
            return outputs.get(nid, "")
        return _REF_RE.sub(repl, prompt)

    # ── persistence ─────────────────────────────────────────────────────

    def _out_dir(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(".letscode", "workflows", ts)

    def _write_render(self) -> str:
        out_dir = self._out_dir()
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "mermaid.md"), "w", encoding="utf-8") as f:
            f.write(f"# Workflow: {self.task}\n\n")
            f.write(self.render())
            f.write("\n\n## Nodes\n\n")
            f.write("| id | kind | card/model | needs |\n")
            f.write("|----|------|------------|-------|\n")
            for n in self._nodes:
                cm = n.card or n.model or "-"
                needs = ", ".join(n.needs) or "-"
                f.write(f"| `{n.id}` | {n.kind} | {cm} | {needs} |\n")
        return out_dir

    def _write_run_log(self, out_dir: str, results: dict[str, str]) -> None:
        with open(os.path.join(out_dir, "run.log"), "w", encoding="utf-8") as f:
            f.write(f"task: {self.task}\n")
            f.write(f"finished_at: {datetime.now().isoformat()}\n")
            f.write("results:\n")
            for n in self._nodes:
                f.write(f"  - id: {n.id}\n")
                f.write(f"    kind: {n.kind}\n")
                f.write(f"    status: {results.get(n.id, n.status)}\n")
                if n.error:
                    f.write(f"    error: {n.error}\n")
                if n.trace:
                    # The dynamic path control flow actually took: which branch
                    # a conditional picked, how many iterations a loop ran.
                    f.write(f"    trace: {json.dumps(n.trace)}\n")

    def _write_outputs(self, out_dir: str) -> None:
        """Persist every node's output text to ``outputs/<id>.md``.

        The synthesize/summary node's text is the user-facing deliverable;
        without this it lived only in memory and was lost when the process
        exited. Per-node files also let a human audit each sub-agent's raw
        contribution after the fact.
        """
        import re as _re
        out_subdir = os.path.join(out_dir, "outputs")
        os.makedirs(out_subdir, exist_ok=True)
        for n in self._nodes:
            if not n.output:
                continue
            # sanitize id for a safe filename (ids are already conservative,
            # but guard against any odd character)
            safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", n.id)
            with open(os.path.join(out_subdir, f"{safe}.md"), "w",
                      encoding="utf-8") as f:
                f.write(f"# Node: {n.id} ({n.kind})\n")
                f.write(f"status: {n.status}\n\n")
                f.write(n.output)


# ── node executors (lazy letscode imports) ──────────────────────────────


def _run_agent(card: str, prompt: str, model: str | None, mcp: bool = False) -> str:
    """Spawn ``letscode --as <card>`` for one node.

    An independent letscode process with its own agent loop, sharing the
    in-use config so the sub-agent gets the same model/API. Captures stdout as
    the node's ``.output`` for downstream interpolation.

    ``mcp``: by default the sub-agent runs ``--no-mcp`` (the Agent-tool
    convention — avoids duplicate MCP connections for pure-code agents like
    Worker). Set ``mcp=True`` for nodes whose card needs MCP tools (e.g. a
    Research card with ``mcp_servers: [exa]``). The card's own
    ``mcp_servers`` whitelist still scopes which servers actually load.
    """
    import subprocess
    import sys as _sys

    cmd = [_sys.executable, "-m", "letscode", "--as", card]
    if not mcp:
        cmd.append("--no-mcp")
    cfg = os.environ.get("LETSCODE_CONFIG")
    if cfg:
        cmd.extend(["--config", cfg])
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"agent {card!r} timed out (900s)") from e
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        # Surface the failure, but still return whatever stdout we got so a
        # downstream summary node can report on it. The node is marked failed
        # via the exception path in _exec_node's caller? — no: we return out
        # and rely on returncode to raise so the node is marked failed.
        raise RuntimeError(
            f"agent {card!r} exit {r.returncode}: "
            f"{(r.stderr or '').strip()[:500]}"
        )
    if not out:
        # rc=0 but empty stdout: the sub-agent exited cleanly without printing
        # a final message (commonly an API error / max-turns / interrupted
        # mid-tool-call). Surface stderr so the cause isn't silent, and mark
        # the node failed — an empty research report is NOT a usable result.
        tail = err[-500:] if err else "(no stderr)"
        raise RuntimeError(
            f"agent {card!r} produced no output (rc=0). stderr tail: {tail}"
        )
    return out


def _run_llm(prompt: str, model: str | None) -> str:
    """Single ``call_llm`` step. Reuses letscode's shared one-shot call."""
    import asyncio

    from letscode.llm import call_llm

    cfg = os.environ.get("LETSCODE_CONFIG")

    async def _go() -> str:
        result = await call_llm(
            [{"type": "text", "text": prompt}],
            system_prompt=(
                "You are a single step in a generated workflow. Answer the "
                "given prompt directly and concisely; your reply is consumed "
                "verbatim as this step's output."
            ),
            model_id=model,
            config_path=cfg,
            purpose="getsmart-llm-node",
        )
        return result.text_content or ""

    try:
        return asyncio.run(_go())
    except Exception as e:
        raise RuntimeError(f"llm node failed: {e}") from e


# ── small helpers ───────────────────────────────────────────────────────


def _id_from_card(card: str) -> str:
    """Derive a readable default id prefix from a card name (``Worker`` → ``worker``)."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", card).strip("_").lower() or "node"
    return base


def _dep_id(dep) -> str:
    """Coerce a ``needs`` entry to its id string: accepts Node or str."""
    if isinstance(dep, Node):
        return dep.id
    return str(dep)


def _mermaid_shape(node: Node) -> tuple[str, str]:
    """(left, right) delimiters encoding the node kind as a Mermaid shape."""
    if node.kind == "agent":
        return ("[", "]")          # box — does work
    if node.kind == "llm":
        return ("(", ")")          # rounded — does work
    if node.kind == "conditional":
        return ("{", "}")          # rhombus — branches
    if node.kind == "loop":
        return ("[/", "\\]")       # parallelogram — repeated body
    return ("{{", "}}")            # hexagon — passthrough / anchor


def _mermaid_label(node: Node) -> str:
    """One-line label for the Mermaid graph: id + kind + card/model/cap."""
    parts = [node.id, node.kind]
    if node.card:
        parts.append(node.card)
    elif node.model:
        parts.append(node.model)
    if node.kind == "loop":
        parts.append(f"×{node.max_iters}")
    label = " · ".join(parts)
    # Mermaid label quoting: escape a double-quote so it doesn't break the node.
    return label.replace('"', "'")
