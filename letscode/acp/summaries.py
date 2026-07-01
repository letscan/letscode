"""Load-time summarization of old conversation turns for the UI.

When a session has many turns, the early ones are collapsed into an LLM
summary so the UI isn't flooded with stale history on load. The summary is
written to the JSONL log as a ``session/summary`` event (UI-only — the agent
replay path skips it, so the agent still sees the full history via ``--feed``).
"""

import sys
import time

from ..feed_util import extract_conversation_text, split_turns
from ..llm import call_llm

#: When a session has more turns than this on load, earlier ones are summarized.
SUMMARY_TURN_THRESHOLD = 20

SUMMARY_EVENT_TYPE = "session/summary"

_SUMMARY_SYSTEM = (
    "Summarize the earlier conversation below concisely. Capture the main "
    "task, key decisions, important files/context, and unresolved questions. "
    "Use the conversation's language. Keep it under ~300 words. Do not invent "
    "details not present in the conversation."
)


def find_summary_event(events: list[dict]) -> dict | None:
    """Return the existing ``session/summary`` event in a log, if any."""
    for ev in events:
        if ev.get("type") == SUMMARY_EVENT_TYPE:
            return ev
    return None


async def summarize_old_turns(
    events: list[dict],
    keep_count: int,
    model_id: str | None,
    config_path: str | None = None,
) -> dict | None:
    """Build a ``session/summary`` event summarizing all but the last ``keep_count`` turns.

    Returns the event dict (with ``type`` set to :data:`SUMMARY_EVENT_TYPE`),
    or None if summarization failed. Does not write to disk — the caller
    persists it.
    """
    turns = split_turns(events)
    if len(turns) <= keep_count:
        return None

    old_turns = turns[:-keep_count] if keep_count > 0 else turns
    summarized_count = len(old_turns)
    transcript = extract_conversation_text(
        [ev for turn in old_turns for ev in turn],
    )
    if not transcript.strip():
        return None

    try:
        result = await call_llm(
            [{"type": "text", "text": transcript}],
            system_prompt=_SUMMARY_SYSTEM,
            model_id=model_id,
            config_path=config_path,
            purpose="summary",
            extra_body={"enable_thinking": False},  # summarization doesn't need reasoning
        )
    except Exception as e:  # noqa: BLE001 — degrade to None, don't crash load
        print(f"[summary] generation failed: {e}", file=sys.stderr)
        return None

    text = (result.text_content or "").strip()
    if not text:
        return None

    return {
        "type": SUMMARY_EVENT_TYPE,
        "timestamp": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "data": {
            "text": text,
            "summarized_turns": summarized_count,
        },
    }
