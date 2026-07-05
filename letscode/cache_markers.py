"""Prompt-cache marker injection for providers that need explicit breakpoints.

DashScope (Qwen) and Anthropic require explicit ``cache_control`` markers on
**content blocks** (not request-level headers) to populate cache stats —
verified in ``docs/cache-probe-2026-07-05.md``. This module attaches those
markers just before the API call, at the single message-assembly site in
``agent.py``.

Strategy — ``system_plus_rolling`` (3 markers, validated in
``docs/cache-multiturn-probe-2026-07-06.md``):

  1. **system message** — marker on its last content block. Covers the stable
     system prompt (the prime cache prefix).
  2. **2nd-to-last non-system message** — the rolling breakpoint that keeps the
     previous turn inside the cached prefix.
  3. **last non-system message** — covers the current turn's new content.

This is the only strategy that achieved 99% cache hit with **zero
``cache_creation``** across turns in our A/B test. The qwen-code strategy
(``system + last only``) was measured to incur non-zero, growing creation cost
on every turn (the Issue #5942 defect) because the moving endpoint forces a
cache rebuild each turn.

Marker mechanics (per Alibaba's explicit-cache best practice):
  - Markers live on content **blocks**, so a message with string ``content``
    must be promoted to ``[{"type":"text","text":..., "cache_control":...}]``.
  - Qwen3.5+ supports only message-level breakpoints — a marker per message,
    max 4 markers per request.
  - All helpers here are idempotent so feed-replay (which rebuilds the messages
    list) never double-marks.
"""

from __future__ import annotations

_MARKER = {"cache_control": {"type": "ephemeral"}}

# Provider hard cap (Qwen/Anthropic): 4 cache_control markers per request.
# We use at most 3 (system + 2 rolling history), leaving headroom.
_MAX_MARKERS = 4


def _promote_to_blocks(msg: dict) -> dict:
    """Return a shallow copy of ``msg`` with string content promoted to a
    single-element text-block list. Messages whose content is already a list
    are returned unchanged. A message with non-string, non-list content (e.g.
    ``None``) is returned unchanged — it can't carry a block marker."""
    content = msg.get("content")
    if isinstance(content, str):
        return {**msg, "content": [{"type": "text", "text": content}]}
    return msg


def _mark_last_block(msg: dict) -> dict:
    """Append ``cache_control`` to the last content block of ``msg``.

    Promotes string content to a block list first. Idempotent: if the last
    block already carries ``cache_control``, the message is returned unchanged.
    No-ops on messages with no markable content (``None`` / empty list)."""
    msg = _promote_to_blocks(msg)
    blocks = msg.get("content")
    if not blocks or not isinstance(blocks, list):
        return msg
    last = blocks[-1]
    if isinstance(last, dict):
        if last.get("cache_control"):
            return msg  # already marked — idempotent
        blocks[-1] = {**last, **_MARKER}
    return msg


def _count_markers(messages: list[dict]) -> int:
    """Total cache_control markers already present across all messages."""
    n = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("cache_control"):
                n += 1
    return n


def apply_cache_markers(messages: list[dict], cache_mode: str) -> list[dict]:
    """Return a new message list with cache markers applied per ``cache_mode``.

    The input list is not mutated (shallow copies via dict spreads and list
    slicing). Modes:

    - ``"auto"`` / ``"none"``: pass-through copy, no markers. Used by
      DeepSeek/GLM which cache automatically on the server side.
    - ``"explicit"``: apply the ``system_plus_rolling`` strategy (see module
      docstring). Used by Qwen/DashScope (and Anthropic when supported).
    """
    # Shallow copy so callers' lists are never mutated.
    msgs = [dict(m) for m in messages]

    if cache_mode not in ("explicit",):
        return msgs

    # System message (index 0) is the stable prefix breakpoint.
    if msgs and msgs[0].get("role") == "system":
        msgs[0] = _mark_last_block(msgs[0])

    # Rolling pair: the last two non-system messages. The 2nd-to-last keeps the
    # previous turn inside the cached prefix (zero cache_creation on the next
    # request); the last covers the current turn's new content.
    non_sys_idx = [i for i, m in enumerate(msgs) if m.get("role") != "system"]
    for i in reversed(non_sys_idx[-2:]):
        if _count_markers(msgs) >= _MAX_MARKERS:
            break
        msgs[i] = _mark_last_block(msgs[i])

    return msgs
