"""Session title generation via an LLM.

The title is derived from the user's prompts only (not the agent's replies) —
the questions a user asks are the most concise signal for what a session is
about. Generated fire-and-forget; failure is non-fatal.
"""

import sys

from ..llm import call_llm

_TITLE_SYSTEM = (
    "Generate a concise title (at most 40 characters) summarizing what the "
    "user is working on, based on their prompts below. Output ONLY the title "
    "text — no quotes, no punctuation marks around it, no explanation. Use "
    "the conversation's language."
)
_MAX_TITLE_LEN = 60   # hard cap on stored title length


async def generate_title(
    user_prompts: list[str],
    model_id: str | None,
    config_path: str | None = None,
) -> str | None:
    """Return a short LLM-generated title, or None on failure.

    ``user_prompts`` are the user's messages so far (first one required). The
    title reflects what the user is asking about — no agent replies needed.
    """
    prompts = [p.strip() for p in (user_prompts or []) if p and p.strip()]
    if not prompts:
        return None

    # Join multiple prompts so a multi-turn session gets a representative title.
    if len(prompts) == 1:
        prompt_text = prompts[0]
    else:
        prompt_text = "\n".join(f"- {p}" for p in prompts)

    try:
        result = await call_llm(
            [{"type": "text", "text": prompt_text}],
            system_prompt=_TITLE_SYSTEM,
            model_id=model_id,
            config_path=config_path,
            purpose="title",
            extra_body={"enable_thinking": False},  # title is trivial — no need to reason
        )
    except Exception as e:  # noqa: BLE001 — fire-and-forget, must not raise
        print(f"[title] generation failed: {e}", file=sys.stderr)
        return None

    # The model may wrap in quotes or add trailing notes after the title; take
    # the first line only, then strip surrounding quotes/punctuation.
    title = (result.text_content or "").split("\n", 1)[0].strip()
    title = title.strip("\"'\u201c\u201d\u2018\u2019").strip()
    if not title:
        return None
    return title[:_MAX_TITLE_LEN]
