"""Let non-vision models "see" images by routing them through a vision model.

When the active model can't handle images (``vision: false``) but a
``vision_model`` is configured, each image in the prompt is sent to that vision
model for a text description. The descriptions are spliced back into the prompt
as plain text, so the text-only main model can reason about image content.

Rewrite layout (the main model sees text only)::

    <user text> [Image-1] <user text> [Image-2] <user text>

    [Image-1] is <vision model's description>
    [Image-2] is <vision model's description>

In-place ``[Image-N]`` markers preserve the original image positions; the
descriptions are appended at the end so they don't fragment the user's prose.
"""

import sys
from pathlib import Path

from .image_store import read_as_data_url
from .llm import call_llm

_VISION_SYSTEM = (
    "You are a vision assistant. Describe the given image concisely in 1-3 "
    "sentences, focusing on what's visually relevant for answering questions "
    "about it. State only what you see; do not speculate beyond the image."
)
_VISION_USER = "Describe this image briefly."

_IMAGE_TYPES = ("image", "image_ref")


def _has_images(blocks: list[dict]) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") in _IMAGE_TYPES
        for b in blocks or []
    )


def _resolve_to_image_block(block: dict) -> dict | None:
    """Normalize an image/image_ref block into an inline ``image`` block.

    image_ref is read from disk and re-encoded to base64. Returns None if the
    referenced file can't be read.
    """
    t = block.get("type")
    if t == "image":
        return block
    if t == "image_ref":
        p = block.get("path")
        if not p:
            return None
        try:
            url = read_as_data_url(Path(p), block.get("mime_type"))
        except OSError:
            return None
        header, _, data = url.partition(",")
        mime = header.split(";")[0].split(":", 1)[-1] if ":" in header else "image/png"
        return {"type": "image", "data": data, "mime_type": mime}
    return None


async def rewrite_prompt_for_text_model(
    prompt_blocks: list[dict],
    vision_model_id: str,
    config_path: str | None = None,
) -> list[dict]:
    """Rewrite image blocks as text using a vision model.

    Returns the (possibly rewritten) prompt blocks. If there are no images, the
    input is returned unchanged. On per-image failure (rate limit, API error,
    unreadable file) the image degrades to a ``[Image-N: <path>]`` reference
    rather than aborting the whole prompt.
    """
    if not _has_images(prompt_blocks):
        return prompt_blocks

    descriptions: list[str] = []   # "[Image-N] is ..." appended at the end
    out: list[dict] = []
    img_index = 0

    for b in prompt_blocks:
        if not (isinstance(b, dict) and b.get("type") in _IMAGE_TYPES):
            out.append(b)
            continue

        img_index += 1
        n = img_index
        marker = f"[Image-{n}]"
        image_block = _resolve_to_image_block(b)

        if image_block is None:
            # File unreadable — degrade to a path reference, no API call.
            ref = b.get("path") or b.get("uri") or "(unavailable)"
            out.append({"type": "text", "text": f"[Image-{n}: {ref}]"})
            continue

        try:
            result = await call_llm(
                [image_block, {"type": "text", "text": _VISION_USER}],
                system_prompt=_VISION_SYSTEM,
                model_id=vision_model_id,
                config_path=config_path,
            )
            desc = (result.text_content or "").strip()
            if not desc:
                desc = "(no description)"
            out.append({"type": "text", "text": marker})
            descriptions.append(f"[Image-{n}] is {desc}")
        except Exception as e:
            # Degrade gracefully: the main model still knows an image was here.
            print(f"[vision] failed to describe image {n}: {e}", file=sys.stderr)
            ref = b.get("path") or b.get("uri") or "(unknown)"
            out.append({"type": "text", "text": f"[Image-{n}: {ref}]"})

    if descriptions:
        out.append({"type": "text", "text": "\n" + "\n".join(descriptions)})

    return out
