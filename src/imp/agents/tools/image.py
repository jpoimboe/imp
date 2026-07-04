"""Provider-agnostic image processing for read_file's vision branch (spec §2).

Pillow does the single resize/re-encode in imp; the processed bytes are reused
by every provider and the stored blob, so payloads stay byte-identical. This
module imports only stdlib + Pillow + the shared content-part types — no
provider — so it is testable standalone.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image

# --- Tunables (Claude Code's numbers) ---------------------------------------
IMAGE_MAX_DIM = 2000                 # longest side after downscale
JPEG_QUALITY_LADDER = (80, 60, 40, 20)
RAW_TARGET_BYTES = 3_750_000         # processed raw ceiling (3.75 MB)
BASE64_TARGET_BYTES = 5_000_000      # base64 ceiling (API hard limit, 5 MB)
IMAGE_READ_CAP_BYTES = 20_000_000    # pre-resize read cap (~20 MB); Part 3 uses it

# Pillow's format name -> (media type, blob extension). Only the four supported
# image formats; anything else Pillow opens is re-encoded down the JPEG path.
_FORMAT_TABLE = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "GIF": ("image/gif", "gif"),
    "WEBP": ("image/webp", "webp"),
}


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    data: bytes          # processed (or passed-through) encoded bytes
    media_type: str      # "image/png" | "image/jpeg" | "image/gif" | "image/webp"
    width: int
    height: int


def _fits(data: bytes) -> bool:
    if len(data) > RAW_TARGET_BYTES:
        return False
    return len(base64.b64encode(data)) <= BASE64_TARGET_BYTES


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def process_image(raw: bytes) -> ProcessedImage:
    img = Image.open(io.BytesIO(raw))
    fmt = img.format  # captured before load/thumbnail reset it
    img.load()
    needs_resize = img.width > IMAGE_MAX_DIM or img.height > IMAGE_MAX_DIM

    # Passthrough: a supported source that already fits and needs no resize
    # keeps its bytes exactly (the stored blob and every provider payload are
    # then byte-identical to the file on disk).
    if not needs_resize and fmt in _FORMAT_TABLE and _fits(raw):
        media_type, _ = _FORMAT_TABLE[fmt]
        return ProcessedImage(raw, media_type, img.width, img.height)

    if needs_resize:
        img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM))
    width, height = img.width, img.height

    if fmt == "PNG":
        png = _encode_png(img)
        if _fits(png):
            return ProcessedImage(png, "image/png", width, height)

    rgb = img.convert("RGB")
    data = b""
    for quality in JPEG_QUALITY_LADDER:
        data = _encode_jpeg(rgb, quality)
        if _fits(data):
            return ProcessedImage(data, "image/jpeg", width, height)
    # Nothing fit even at the lowest quality: return the smallest we produced.
    return ProcessedImage(data, "image/jpeg", width, height)
