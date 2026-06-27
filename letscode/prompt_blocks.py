"""Prompt block preprocessing.

Image content blocks arrive over ACP carrying inline base64 data. The agent
harness is model-agnostic and tools (Read, MCP, skills) consume local files,
so image blocks are materialized to disk and rewritten as path references
(plain text blocks) before the prompt reaches the LLM or the event log.

Non-image blocks pass through unchanged. If no image block is present this is
a no-op, leaving the common text-only path untouched.
"""

import base64
import hashlib
import os
from pathlib import Path

# mime_type -> file extension, for the spilled filename.
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
}


def _ext_for(mime: str | None) -> str:
    """Return a file extension for a mime type (default 'img')."""
    if mime:
        ext = _MIME_EXT.get(mime.lower().strip())
        if ext:
            return ext
    return "img"


def default_images_dir() -> Path:
    """The directory spilled prompt images live in for the current cwd.

    Sits alongside other per-cwd metadata under ``.letscode/`` so it stays
    hidden from the user's tree while remaining readable by the agent's tools.
    """
    d = Path(os.getcwd()) / ".letscode" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def materialize_blocks(
    blocks: list[dict], *, images_dir: Path | None = None,
) -> list[dict]:
    """Return prompt blocks with image blocks spilled to local files.

    Each ``{"type": "image", "data": <base64>, ...}`` block is decoded and
    written to ``<images_dir>/<sha256>.<ext>`` (sha256 of the decoded bytes =>
    deterministic filename, so replaying the same prompt or feed does not
    create duplicates). The block is replaced by a text block referencing the
    absolute path::

        {"type": "text", "text": "Image: /abs/path/to/file.png"}

    Non-image blocks are returned as-is. The default ``images_dir`` is
    :func:`default_images_dir`.
    """
    if not isinstance(blocks, list):
        return blocks

    out: list[dict] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "image":
            out.append(_materialize_image(b, images_dir or default_images_dir()))
        else:
            out.append(b)
    return out


def _materialize_image(block: dict, images_dir: Path) -> dict:
    """Spill one image block to disk; return a text block referencing it."""
    data = block.get("data")
    if not data or not isinstance(data, str):
        # Unusable payload — degrade gracefully rather than crash the prompt.
        uri = block.get("uri")
        note = f"Image (unavailable): {uri}" if uri else "Image (unavailable)"
        return {"type": "text", "text": note}

    try:
        raw = base64.b64decode(data, validate=False)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        uri = block.get("uri")
        note = f"Image (unavailable): {uri}" if uri else "Image (unavailable)"
        return {"type": "text", "text": note}

    mime = block.get("mime_type") or block.get("mimeType")
    ext = _ext_for(mime)
    digest = hashlib.sha256(raw).hexdigest()[:32]
    path = (images_dir / f"{digest}.{ext}")
    if not path.exists():
        images_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return {"type": "text", "text": f"Image: {path.resolve()}"}
