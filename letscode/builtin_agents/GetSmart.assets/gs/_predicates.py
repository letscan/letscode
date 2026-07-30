"""Restricted predicate DSL for conditional/loop control flow in workflows.

This is the "control-flow condition" language for GetSmart's conditional and
loop primitives. It is deliberately **not Turing-complete** — predicates are
static string patterns evaluated against a node's captured output, with no
arbitrary code execution. This keeps control-flow decisions analyzable at
``validate()`` time (every predicate parses, nothing runs) and upholds the
"keep control-flow deterministic" principle: branch/stop decisions depend on
real agent/LLM outputs plus a fixed predicate, never on an LLM self-report.

Grammar (lowest → highest precedence)::

    expr  := or
    or    := and ( '||' and )*
    and   := not ( '&&' not )*
    not   := '!' not | atom
    atom  := 'always'
          | 'empty' | 'nonempty'
          | 'contains:' STR | 'not-contains:' STR
          | 'matches:' REGEX
          | 'equals:' STR
          | '(' expr ')'

Atoms:
    contains:KEYWORD     output.find(KEYWORD) != -1
    not-contains:KEYWORD KEYWORD not in output
    matches:REGEX        re.search(REGEX, output) is not None   (POSIX-ish)
    equals:STR           output == STR
    empty                output == ""
    nonempty             output != ""
    always               True   (useful for loop "run exactly N times" via max_iters)

Combining operators ``&&``, ``||``, ``!`` and parentheses compose atoms.
The text after a ``:`` runs to the next top-level operator or close-paren
(so ``contains:a && b`` means "contains a" AND bare atom "b" — quote/spaces
matter: write ``contains:a&&b`` only if you mean the literal "a&&b"). To keep
predicates readable, prefer whitespace around ``&&``/``||``.

Public API:
    parse(dsl)    -> callable[[str], bool]   (raises PredicateError on bad syntax)
    evaluate(dsl, output) -> bool            (parse + call; raises PredicateError)
"""

from __future__ import annotations

import re

__all__ = ["parse", "evaluate", "PredicateError"]


class PredicateError(Exception):
    """Malformed predicate DSL (syntax error / unknown atom)."""


# ── tokenizer ───────────────────────────────────────────────────────────
# We split on the combining operators and parentheses while keeping them as
# tokens. Atom bodies (everything after `keyword:` up to the next operator)
# are captured raw — a regex/substring may legally contain spaces.

_OP_RE = re.compile(r"\s*(\|\||&&|[()!])\s*")


def _tokens(s: str) -> list[str]:
    """Split a predicate into operator/atom tokens, preserving atom text.

    Atoms are recognized lazily: anything that isn't an operator/paren is an
    atom token, validated later by :func:`_eval_atom`.
    """
    out: list[str] = []
    pos = 0
    for m in _OP_RE.finditer(s):
        if m.start() > pos:
            piece = s[pos:m.start()].strip()
            if piece:
                out.append(piece)
        out.append(m.group(1))
        pos = m.end()
    tail = s[pos:].strip()
    if tail:
        out.append(tail)
    return out


# ── recursive-descent parser → AST (nested tuples) ──────────────────────
#
# AST node shapes:
#   ("or",  a, b) | ("and", a, b) | ("not", a) | ("atom", text)
#
# We build tuples rather than closures so parse() can fully validate structure
# without evaluating — and the closure is built once at the end.

def _parse_or(toks, i):
    node, i = _parse_and(toks, i)
    while i < len(toks) and toks[i] == "||":
        rhs, i = _parse_and(toks, i + 1)
        node = ("or", node, rhs)
    return node, i


def _parse_and(toks, i):
    node, i = _parse_not(toks, i)
    while i < len(toks) and toks[i] == "&&":
        rhs, i = _parse_not(toks, i + 1)
        node = ("and", node, rhs)
    return node, i


def _parse_not(toks, i):
    if i < len(toks) and toks[i] == "!":
        inner, i = _parse_not(toks, i + 1)
        return ("not", inner), i
    return _parse_atom(toks, i)


def _parse_atom(toks, i):
    if i >= len(toks):
        raise PredicateError("expected an atom, found end of expression")
    t = toks[i]
    if t == "(":
        node, j = _parse_or(toks, i + 1)
        if j >= len(toks) or toks[j] != ")":
            raise PredicateError("unbalanced parenthesis (missing ')')")
        return node, j + 1
    if t in ("&&", "||", ")", "!"):
        raise PredicateError(f"unexpected operator {t!r} where an atom was expected")
    # A bare atom — validate its keyword:value form now so parse() rejects
    # nonsense like "matches:[unclosed" early (regex compile check).
    _eval_atom(t, "")  # validate keyword + regex compilability; value irrelevant
    return ("atom", t), i + 1


def _build(ast):
    """Compile an AST tuple into a ``Callable[[str], bool]``."""
    tag = ast[0]
    if tag == "atom":
        text = ast[1]
        return lambda out, _t=text: _eval_atom(_t, out)
    if tag == "not":
        inner = _build(ast[1])
        return lambda out: not inner(out)
    if tag == "and":
        a, b = _build(ast[1]), _build(ast[2])
        return lambda out: a(out) and b(out)
    if tag == "or":
        a, b = _build(ast[1]), _build(ast[2])
        return lambda out: a(out) or b(out)
    raise PredicateError(f"bad AST node: {ast!r}")  # unreachable


def parse(dsl: str):
    """Parse a predicate DSL string into a ``Callable[[str], bool]``.

    Raises :class:`PredicateError` on any syntax error, unknown atom keyword,
    or uncompilable regex. The returned callable raises ``PredicateError``
    only if the regex itself is bad — but that's caught at parse time, so in
    practice the callable never raises.
    """
    if not isinstance(dsl, str) or not dsl.strip():
        raise PredicateError("empty predicate")
    toks = _tokens(dsl)
    if not toks:
        raise PredicateError("empty predicate")
    ast, i = _parse_or(toks, 0)
    if i != len(toks):
        raise PredicateError(
            f"unexpected trailing tokens: {toks[i:]!r} (unbalanced paren?)"
        )
    return _build(ast)


def evaluate(dsl: str, output: str) -> bool:
    """Parse ``dsl`` and evaluate it against ``output``. One-shot convenience."""
    return parse(dsl)(output)


# ── atom evaluation ─────────────────────────────────────────────────────

def _eval_atom(text: str, output: str) -> bool:
    """Evaluate a single atom token against ``output``.

    ``text`` is a raw atom like ``contains:foo`` or ``always``. Raises
    :class:`PredicateError` for unknown keywords or bad regexes — this is
    what makes ``parse()`` a full syntactic validation.
    """
    # Keyword atoms with no payload.
    if text == "always":
        return True
    if text == "empty":
        return output == ""
    if text == "nonempty":
        return output != ""

    # Keyword:value atoms. Split on the FIRST colon only so the value may
    # itself contain colons (e.g. equals:http://x).
    if ":" not in text:
        raise PredicateError(
            f"unknown atom {text!r} (expected contains:/matches:/equals:/... "
            "or always/empty/nonempty)"
        )
    kw, value = text.split(":", 1)
    kw = kw.strip().lower()
    if kw == "contains":
        return value in output
    if kw == "not-contains":
        return value not in output
    if kw == "equals":
        return output == value
    if kw == "matches":
        try:
            return re.search(value, output) is not None
        except re.error as e:
            raise PredicateError(f"bad regex in {text!r}: {e}") from e
    raise PredicateError(f"unknown atom keyword {kw!r} in {text!r}")
