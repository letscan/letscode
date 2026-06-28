"""Tests for the vision proxy — rewriting image blocks via a vision model."""

import asyncio
import base64
from unittest.mock import AsyncMock, patch

from letscode.stream import StreamResult
from letscode.vision_proxy import rewrite_prompt_for_text_model


def _png_bytes() -> bytes:
    import struct, zlib
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00")) + chunk(b"IEND", b"")


class TestRewriteForTextModel:
    """Image blocks become text via the vision model; failures degrade to refs."""

    def test_no_images_is_noop(self):
        blocks = [{"type": "text", "text": "hello"}]

        async def run():
            return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())
        assert out is blocks  # unchanged, same object

    def test_image_replaced_with_marker_and_description_appended(self, tmp_path):
        img_path = tmp_path / "a.png"
        img_path.write_bytes(_png_bytes())
        blocks = [
            {"type": "image_ref", "path": str(img_path), "mime_type": "image/png"},
            {"type": "text", "text": "describe this"},
        ]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="a red square", tool_calls=[])
            )):
                return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())

        # No image blocks remain — all text.
        assert all(b["type"] == "text" for b in out), out
        # In-place marker
        assert out[0]["text"] == "[Image-1]"
        # User text preserved
        assert out[1]["text"] == "describe this"
        # Description appended at end
        assert "[Image-1] is a red square" in out[-1]["text"]

    def test_multiple_images_numbered_sequentially(self, tmp_path):
        a = tmp_path / "a.png"; a.write_bytes(_png_bytes())
        b = tmp_path / "b.png"; b.write_bytes(_png_bytes())
        blocks = [
            {"type": "image_ref", "path": str(a)},
            {"type": "text", "text": "and"},
            {"type": "image_ref", "path": str(b)},
        ]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="desc", tool_calls=[])
            )):
                return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())
        markers = [b["text"] for b in out if b["text"].startswith("[Image-")]
        assert markers == ["[Image-1]", "[Image-2]"], markers
        # Both descriptions in the appended block
        assert "[Image-1] is desc" in out[-1]["text"]
        assert "[Image-2] is desc" in out[-1]["text"]

    def test_inline_image_block_also_handled(self):
        data = base64.b64encode(_png_bytes()).decode()
        blocks = [
            {"type": "image", "data": data, "mime_type": "image/png"},
            {"type": "text", "text": "what?"},
        ]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="a shape", tool_calls=[])
            )):
                return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())
        assert out[0]["text"] == "[Image-1]"
        assert "[Image-1] is a shape" in out[-1]["text"]

    def test_vision_call_failure_degrades_to_path_ref(self, tmp_path):
        img_path = tmp_path / "a.png"
        img_path.write_bytes(_png_bytes())
        blocks = [{"type": "image_ref", "path": str(img_path)}]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock(
                side_effect=RuntimeError("rate limited")
            )):
                return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())
        # Degraded to a path reference, not a crash.
        assert out[0]["text"] == f"[Image-1: {img_path}]"
        # No description block appended (all failed).
        assert len(out) == 1

    def test_empty_description_uses_placeholder(self, tmp_path):
        img_path = tmp_path / "a.png"
        img_path.write_bytes(_png_bytes())
        blocks = [{"type": "image_ref", "path": str(img_path)}]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock(
                return_value=StreamResult(text_content="", tool_calls=[])
            )):
                return await rewrite_prompt_for_text_model(blocks, "vm", None)
        out = asyncio.run(run())
        assert "[Image-1] is (no description)" in out[-1]["text"]

    def test_unreadable_image_ref_degrades_without_api_call(self, tmp_path):
        # File doesn't exist — should degrade without calling the vision model.
        blocks = [{"type": "image_ref", "path": str(tmp_path / "nope.png")}]

        async def run():
            with patch("letscode.vision_proxy.call_llm", new=AsyncMock()) as m:
                out = await rewrite_prompt_for_text_model(blocks, "vm", None)
                assert m.call_count == 0  # no API call for unreadable file
                return out
        out = asyncio.run(run())
        assert out[0]["text"].startswith("[Image-1:")
