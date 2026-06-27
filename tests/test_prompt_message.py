"""Tests for the prompt content-block pipeline.

The chain is:  CLI args → blocks → OpenAI message  (and → text summary).

    _build_prompt_blocks   (letscode.cli)         args → blocks
    spill_image / read_as_data_url (image_store)  image data ↔ on-disk file
    _prompt_message        (letscode.subscribers) blocks → OpenAI user message
    blocks_text_summary    (letscode.subscribers) blocks → flat text (logs/compact)

These assert STRUCTURE (part order, count, types) in addition to content —
content-only assertions previously let a text-first/image-last flattening bug
slip through to E2E.
"""

import base64
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

from letscode.cli import _build_prompt_blocks
from letscode.image_store import spill_image, read_as_data_url, ext_for, mime_for_ext
from letscode.subscribers import _prompt_message, blocks_text_summary


def _png_bytes() -> bytes:
    """A minimal valid 2x2 red PNG, for round-tripping through spill/resolve."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)  # 2x2, 8-bit RGB
    row = b"\x00" + b"\xff\x00\x00" * 2  # filter=none + 2 red pixels
    idat = zlib.compress(row)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _part_types(msg: dict) -> list[str]:
    """Part-type list of a message's content; plain-string content → ['text']."""
    c = msg["content"]
    if isinstance(c, str):
        return ["text"]
    return [p["type"] for p in c]


# ---------------------------------------------------------------------------
# CLI args → blocks
# ---------------------------------------------------------------------------

class TestBuildPromptBlocks:
    """`_build_prompt_blocks` turns CLI input into ordered content blocks.

    Order is taken from sys.argv (argparse's `append` lists lose interleaving),
    and a positional argument is always the trailing text block.
    """

    def _args(self, prompt=None, text=None, image=None) -> SimpleNamespace:
        return SimpleNamespace(prompt=prompt, text=text or [], image=image or [])

    def test_positional_only_is_single_text(self, monkeypatch):
        # The common path `letscode "..."` is unchanged.
        monkeypatch.setattr("sys.argv", ["letscode", "修这个bug"])
        blocks = _build_prompt_blocks(self._args(prompt="修这个bug"))
        assert blocks == [{"type": "text", "text": "修这个bug"}]

    def test_no_prompt_no_flags_is_empty_text(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["letscode"])
        blocks = _build_prompt_blocks(self._args())
        assert blocks == [{"type": "text", "text": ""}]

    def test_flags_preserve_command_line_order(self, monkeypatch, tmp_path):
        # --text/--image must interleave in argv order, not be grouped by type.
        img = tmp_path / "a.png"
        img.write_bytes(b"")
        monkeypatch.setattr(
            "sys.argv",
            ["letscode", "--text", "t1", "--image", str(img), "--text", "t2"],
        )
        blocks = _build_prompt_blocks(
            self._args(text=["t1", "t2"], image=[str(img)]),
        )
        kinds = [b["type"] for b in blocks]
        assert kinds == ["text", "image_ref", "text"], kinds

    def test_positional_appended_as_trailing_text(self, monkeypatch, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"")
        monkeypatch.setattr(
            "sys.argv",
            ["letscode", "--text", "看这张", "--image", str(img), "重点右下"],
        )
        blocks = _build_prompt_blocks(
            self._args(prompt="重点右下", text=["看这张"], image=[str(img)]),
        )
        assert [b["type"] for b in blocks] == ["text", "image_ref", "text"]
        assert blocks[-1] == {"type": "text", "text": "重点右下"}

    def test_image_path_resolved_to_absolute(self, monkeypatch, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["letscode", "--image", "a.png"])
        blocks = _build_prompt_blocks(self._args(image=["a.png"]))
        assert blocks[0]["type"] == "image_ref"
        assert Path(blocks[0]["path"]).is_absolute()
        assert Path(blocks[0]["path"]).name == "a.png"

    def test_multiple_images_interleave(self, monkeypatch, tmp_path):
        a = tmp_path / "a.png"; a.write_bytes(b"")
        b = tmp_path / "b.png"; b.write_bytes(b"")
        monkeypatch.setattr(
            "sys.argv",
            ["letscode", "--image", str(a), "--text", "mid", "--image", str(b)],
        )
        blocks = _build_prompt_blocks(
            self._args(text=["mid"], image=[str(a), str(b)]),
        )
        assert [b["type"] for b in blocks] == ["image_ref", "text", "image_ref"]


# ---------------------------------------------------------------------------
# image_store: spill + read_as_data_url
# ---------------------------------------------------------------------------

class TestImageStore:
    """Images are content-addressed (idempotent spill) and resolve back exactly."""

    def test_ext_for_known_and_unknown(self):
        assert ext_for("image/png") == "png"
        assert ext_for("image/jpeg") == "jpg"
        assert ext_for("IMAGE/GIF") == "gif"  # case-insensitive
        assert ext_for(None) == "img"
        assert ext_for("image/x-weird") == "img"

    def test_mime_for_ext_round_trips(self):
        assert mime_for_ext(Path("x.png")) == "image/png"
        assert mime_for_ext(Path("x.jpg")) == "image/jpeg"
        # unknown → png default
        assert mime_for_ext(Path("x.unknown")) == "image/png"

    def test_spill_writes_file_and_is_idempotent(self, tmp_path):
        raw = _png_bytes()
        data = base64.b64encode(raw).decode()
        images_dir = tmp_path / "imgs"

        p1 = spill_image(data, "image/png", images_dir=images_dir)
        assert p1.exists() and p1.suffix == ".png"
        assert p1.read_bytes() == raw

        # Same content → same path, no second file written.
        files_before = set(images_dir.glob("*"))
        p2 = spill_image(data, "image/png", images_dir=images_dir)
        assert p2 == p1
        assert set(images_dir.glob("*")) == files_before

    def test_spill_different_content_different_path(self, tmp_path):
        d1 = base64.b64encode(b"AAA").decode()
        d2 = base64.b64encode(b"BBB").decode()
        p1 = spill_image(d1, "image/png", images_dir=tmp_path)
        p2 = spill_image(d2, "image/png", images_dir=tmp_path)
        assert p1 != p2

    def test_read_as_data_url_round_trips(self, tmp_path):
        raw = _png_bytes()
        p = tmp_path / "x.png"
        p.write_bytes(raw)
        url = read_as_data_url(p, "image/png")
        assert url.startswith("data:image/png;base64,")
        decoded = base64.b64decode(url.split("base64,", 1)[1])
        assert decoded == raw

    def test_read_as_data_url_guesses_mime_from_extension(self, tmp_path):
        p = tmp_path / "x.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0")
        url = read_as_data_url(p)
        assert url.startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# blocks → OpenAI message (the critical structural assertions)
# ---------------------------------------------------------------------------

class TestPromptMessage:
    """`_prompt_message` must preserve block order in the OpenAI content parts.

    This is where the text-first/image-last flattening bug lived: a content-only
    test passed because the data URL was right, but the part ORDER was wrong for
    multi-image prompts. These tests assert order, count, and types.
    """

    def test_text_only_is_plain_string(self):
        msg = _prompt_message([{"type": "text", "text": "hi"}])
        assert msg == {"role": "user", "content": "hi"}
        assert isinstance(msg["content"], str)

    def test_adjacent_text_blocks_merge(self):
        msg = _prompt_message([
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ])
        # No image → still a plain string, concatenated.
        assert msg == {"role": "user", "content": "ab"}

    def test_image_only_no_empty_text_part(self, tmp_path):
        p = tmp_path / "a.png"
        p.write_bytes(_png_bytes())
        msg = _prompt_message([{"type": "image_ref", "path": str(p)}])
        assert _part_types(msg) == ["image_url"]
        # No stray empty text part must be emitted around the image.
        assert not any(
            pp["type"] == "text" and pp["text"] == "" for pp in msg["content"]
        )

    def test_inline_image_becomes_data_url(self):
        data = base64.b64encode(_png_bytes()).decode()
        msg = _prompt_message([
            {"type": "text", "text": "q"},
            {"type": "image", "data": data, "mime_type": "image/png"},
        ])
        assert _part_types(msg) == ["text", "image_url"]
        url = msg["content"][1]["image_url"]["url"]
        assert url == f"data:image/png;base64,{data}"

    def test_image_ref_becomes_data_url(self, tmp_path):
        p = tmp_path / "a.png"
        p.write_bytes(_png_bytes())
        msg = _prompt_message([
            {"type": "text", "text": "q"},
            {"type": "image_ref", "path": str(p), "mime_type": "image/png"},
        ])
        assert _part_types(msg) == ["text", "image_url"]
        url = msg["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split("base64,", 1)[1]) == _png_bytes()

    def test_zero_regression_image_ref_equals_inline(self, tmp_path):
        # The whole point of image_ref: vision models must see the SAME message
        # as if the image had been inline all along.
        raw = _png_bytes()
        p = tmp_path / "a.png"
        p.write_bytes(raw)
        data = base64.b64encode(raw).decode()
        inline = _prompt_message([{"type": "image", "data": data, "mime_type": "image/png"}])
        ref = _prompt_message([{"type": "image_ref", "path": str(p), "mime_type": "image/png"}])
        assert inline == ref

    def test_multi_image_preserves_interleave_order(self, tmp_path):
        # THE regression test: t1, img, t2, img, tail must NOT flatten to
        # text + image + image.
        a = tmp_path / "a.png"; a.write_bytes(_png_bytes())
        b = tmp_path / "b.png"; b.write_bytes(_png_bytes())
        msg = _prompt_message([
            {"type": "text", "text": "t1"},
            {"type": "image_ref", "path": str(a)},
            {"type": "text", "text": "t2"},
            {"type": "image_ref", "path": str(b)},
            {"type": "text", "text": "tail"},
        ])
        assert _part_types(msg) == [
            "text", "image_url", "text", "image_url", "text",
        ]
        assert msg["content"][0]["text"] == "t1"
        assert msg["content"][2]["text"] == "t2"
        assert msg["content"][4]["text"] == "tail"

    def test_adjacent_text_around_image_merges(self, tmp_path):
        p = tmp_path / "a.png"; p.write_bytes(_png_bytes())
        msg = _prompt_message([
            {"type": "text", "text": "pre1"},
            {"type": "text", "text": "pre2"},
            {"type": "image_ref", "path": str(p)},
            {"type": "text", "text": "post1"},
            {"type": "text", "text": "post2"},
        ])
        assert _part_types(msg) == ["text", "image_url", "text"]
        assert msg["content"][0]["text"] == "pre1pre2"
        assert msg["content"][2]["text"] == "post1post2"

    def test_camelcase_mimetype_handled(self):
        data = base64.b64encode(_png_bytes()).decode()
        msg = _prompt_message([{"type": "image", "data": data, "mimeType": "image/png"}])
        assert msg["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_missing_image_ref_file_skipped_not_crash(self, tmp_path):
        msg = _prompt_message([
            {"type": "text", "text": "q"},
            {"type": "image_ref", "path": str(tmp_path / "nope.png")},
        ])
        # Missing image is dropped; remaining text still produces a message.
        assert msg == {"role": "user", "content": "q"}

    def test_empty_image_data_skipped(self):
        msg = _prompt_message([
            {"type": "text", "text": "q"},
            {"type": "image", "data": "", "mime_type": "image/png"},
        ])
        assert msg == {"role": "user", "content": "q"}


# ---------------------------------------------------------------------------
# blocks → text summary (ref trace preserved)
# ---------------------------------------------------------------------------

class TestBlocksTextSummary:
    """Image blocks leave a trace in logs/compact transcripts without polluting
    the LLM message (which still gets a real image_url)."""

    def test_text_only_concatenates(self):
        assert blocks_text_summary([
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]) == "ab"

    def test_image_ref_leaves_path_trace(self):
        s = blocks_text_summary([
            {"type": "text", "text": "看"},
            {"type": "image_ref", "path": "/abs/x.png"},
        ])
        assert s == "看[image: /abs/x.png]"

    def test_inline_image_leaves_mime_size_trace(self):
        s = blocks_text_summary([
            {"type": "image", "data": "AAAA", "mime_type": "image/png"},
        ])
        assert s == "[image: image/png 4B]"

    def test_empty_blocks(self):
        assert blocks_text_summary([]) == ""
        assert blocks_text_summary(None) == ""
