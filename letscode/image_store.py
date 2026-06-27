"""Image persistence and resolution for prompt content blocks.

Two roles:

1. *Spill* an inline image (base64 ``data``) to a local file so it can travel
   between the ACP server and the CLI subprocess as a short path reference
   (``image_ref``) instead of a huge base64 blob in argv — which would blow
   past ``ARG_MAX`` for real screenshots.

2. *Resolve* a path reference back into an inline ``data:<mime>;base64,...``
   data URL when building the OpenAI message, so vision models receive the
   image exactly as if it had been inline all along.

Spilling is content-addressed (sha256 of the decoded bytes), so writing the
same image twice — or replaying a feed — produces the same path and never
duplicates a file.
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

# Reverse map: extension -> mime_type, used when resolving a path with no mime.
_EXT_MIME: dict[str, str] = {
    ext: mime for mime, ext in _MIME_EXT.items() if ext not in {"jpg"}
}
_EXT_MIME["jpg"] = "image/jpeg"
_EXT_MIME["svg"] = "image/svg+xml"


def ext_for(mime: str | None) -> str:
    """Return a file extension for a mime type (default ``img``)."""
    if mime:
        ext = _MIME_EXT.get(mime.lower().strip())
        if ext:
            return ext
    return "img"


def mime_for_ext(path: Path) -> str:
    """Guess a mime type from a path's extension (default ``image/png``)."""
    return _EXT_MIME.get(path.suffix.lower().lstrip("."), "image/png")


def default_images_dir() -> Path:
    """Directory spilled prompt images live in for the current cwd.

    Sits alongside other per-cwd metadata under ``.letscode/`` so it stays
    hidden from the user's tree while remaining readable by the agent's tools.
    """
    d = Path(os.getcwd()) / ".letscode" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spill_image(
    data: str, mime_type: str, *, images_dir: Path | None = None,
) -> Path:
    """Decode ``data`` (base64) and write it to a content-addressed file.

    The filename is ``<sha256[:32]>.<ext>`` of the decoded bytes, so the same
    image always lands at the same path — idempotent across calls and across
    feed replay. Returns the absolute path of the written file.
    """
    raw = base64.b64decode(data, validate=False)
    digest = hashlib.sha256(raw).hexdigest()[:32]
    images_dir = images_dir or default_images_dir()
    path = images_dir / f"{digest}.{ext_for(mime_type)}"
    if not path.exists():
        images_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return path.resolve()


def read_as_data_url(path: Path, mime_type: str | None = None) -> str:
    """Read ``path`` and return a ``data:<mime>;base64,<...>`` URL.

    ``mime_type`` wins if given; otherwise it is guessed from the extension.
    Used when building OpenAI ``image_url`` parts from ``image_ref`` blocks.
    """
    raw = Path(path).read_bytes()
    mime = mime_type or mime_for_ext(Path(path))
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
