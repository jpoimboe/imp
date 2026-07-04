import base64
import io
import os

from PIL import Image

from imp.agents.runner import ContentPart, ImagePart, TextPart
from imp.agents.tools import image as imgmod


def test_text_part_defaults_type_and_holds_text():
    part = TextPart(text="login.png — 1200×800 PNG")
    assert part.type == "text"
    assert part.text == "login.png — 1200×800 PNG"


def test_image_part_carries_wire_and_persistence_fields():
    part = ImagePart(
        media_type="image/png",
        data="aGVsbG8=",
        width=1200,
        height=800,
        blob="deadbeef.png",
        nbytes=340_000,
    )
    assert part.type == "image"
    assert part.media_type == "image/png"
    assert part.data == "aGVsbG8="
    assert (part.width, part.height) == (1200, 800)
    assert part.blob == "deadbeef.png"
    assert part.nbytes == 340_000


def test_image_part_is_frozen():
    import dataclasses
    import pytest

    part = ImagePart(
        media_type="image/jpeg",
        data="Zm9v",
        width=10,
        height=10,
        blob="cafe.jpg",
        nbytes=3,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.data = "changed"  # type: ignore[misc]


def test_text_part_is_frozen():
    import dataclasses
    import pytest

    part = TextPart(text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.text = "changed"  # type: ignore[misc]


def test_content_part_union_admits_both():
    parts: list[ContentPart] = [TextPart(text="hi"), ImagePart(
        media_type="image/png", data="Zm9v", width=1, height=1,
        blob="a.png", nbytes=3,
    )]
    assert [p.type for p in parts] == ["text", "image"]


def _png_bytes(w: int, h: int, color=(10, 20, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _noise_png_bytes(w: int, h: int) -> bytes:
    # os.urandom pixels: incompressible, so PNG blows past the 3.75 MB raw
    # budget and forces the JPEG ladder.
    im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _img_bytes(fmt: str, w: int, h: int, mode="RGB", color=(10, 20, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, format=fmt)
    return buf.getvalue()


def test_small_png_passes_through_unchanged():
    raw = _png_bytes(4, 4)
    out = imgmod.process_image(raw)
    assert out.media_type == "image/png"
    assert out.data == raw  # byte-identical: not re-encoded
    assert (out.width, out.height) == (4, 4)


def test_small_jpeg_passes_through_unchanged():
    raw = _img_bytes("JPEG", 8, 8)
    out = imgmod.process_image(raw)
    assert out.media_type == "image/jpeg"
    assert out.data == raw  # byte-identical: not re-encoded
    assert (out.width, out.height) == (8, 8)


def test_small_gif_passes_through_unchanged():
    raw = _img_bytes("GIF", 8, 8, mode="P")
    out = imgmod.process_image(raw)
    assert out.media_type == "image/gif"
    assert out.data == raw  # byte-identical: not re-encoded
    assert (out.width, out.height) == (8, 8)


def test_small_webp_passes_through_unchanged():
    raw = _img_bytes("WEBP", 8, 8)
    out = imgmod.process_image(raw)
    assert out.media_type == "image/webp"
    assert out.data == raw  # byte-identical: not re-encoded
    assert (out.width, out.height) == (8, 8)


def test_oversized_image_downscales_preserving_aspect():
    raw = _png_bytes(3000, 1000, color=(0, 128, 0))
    out = imgmod.process_image(raw)
    assert max(out.width, out.height) == 2000
    # 3000x1000 -> 2000x667 (aspect preserved, longest side capped at 2000)
    assert out.width == 2000
    assert out.height == 667


def test_incompressible_oversized_image_walks_jpeg_ladder_and_fits():
    raw = _noise_png_bytes(2500, 2500)
    out = imgmod.process_image(raw)
    assert out.media_type == "image/jpeg"          # PNG rejected -> JPEG
    assert (out.width, out.height) == (2000, 2000)  # also downscaled
    assert len(out.data) < len(raw)                 # genuinely smaller
    assert len(out.data) <= imgmod.RAW_TARGET_BYTES
    assert len(base64.b64encode(out.data)) <= imgmod.BASE64_TARGET_BYTES
