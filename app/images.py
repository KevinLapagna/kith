"""Image import pipeline: make an uploaded photo safe, small, and searchable.

The pipeline is deliberately destructive and one-way:
  * EXIF orientation is APPLIED, then the metadata is dropped — a personal photo library
    should not carry GPS coordinates into an app database, and stripping it is free here
    because we re-encode anyway.
  * Everything is downscaled to a display-ready size (Q10: settled — no originals kept).
  * A 64-bit difference hash is computed for near-duplicate detection and, later, for
    picking *plausible distractors* in the trainer (visually similar faces make better
    wrong answers than random ones).

`Image.MAX_IMAGE_PIXELS` is lowered from Pillow's default so a decompression bomb is
rejected before it can allocate gigabytes.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from app import i18n

from PIL import Image, ImageOps, UnidentifiedImageError

# The largest thing the app ever renders is a tablet in portrait, so 1024 is generous.
MAX_EDGE = 1024
THUMB_EDGE = 256
WEBP_QUALITY = 80
THUMB_QUALITY = 72

# Pillow raises DecompressionBombError above this. 40 MP is far beyond any phone camera.
Image.MAX_IMAGE_PIXELS = 40_000_000

MIME = "image/webp"


class ImageRejected(ValueError):
    """The upload is not an image we can use. The message is shown to the user."""


@dataclass(frozen=True)
class Imported:
    data: bytes
    thumb: bytes
    mime: str
    width: int
    height: int
    bytes: int
    sha256: str
    dhash: str


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _open(raw: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except UnidentifiedImageError as exc:
        raise ImageRejected(i18n.t("error.not_an_image")) from exc
    except Image.DecompressionBombError as exc:
        raise ImageRejected(i18n.t("error.image_too_large")) from exc
    except (OSError, ValueError) as exc:
        raise ImageRejected(i18n.t("error.could_not_read_image", detail=exc)) from exc

    # Apply the EXIF orientation now; the re-encode below drops the tag entirely.
    image = ImageOps.exif_transpose(image)

    # Flatten transparency onto white rather than letting it turn black in RGB.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, (255, 255, 255))
        canvas.paste(image, mask=image.split()[-1])
        return canvas
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _fit(image: Image.Image, max_edge: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return copy


def _encode(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=quality, method=4)
    return buffer.getvalue()


def dhash(image: Image.Image, size: int = 8) -> str:
    """64-bit difference hash as hex — cheap, dependency-free, good enough for "alike"."""
    small = image.convert("L").resize((size + 1, size), Image.LANCZOS)
    # `tobytes()` on an "L" image is the same row-major, 8-bit-per-pixel data that
    # `list(getdata())` produced — Pillow 12 deprecates the latter (removed in 14),
    # and this skips building a list of Python ints on the way.
    pixels = small.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hamming(hex_a: str, hex_b: str) -> int:
    """Number of differing bits between two hashes. <= ~10 usually means "same face-ish"."""
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def import_image(raw: bytes) -> Imported:
    """Turn uploaded bytes into the two WebP renditions we store, plus fingerprints."""
    if not raw:
        raise ImageRejected(i18n.t("error.empty_image"))
    image = _open(raw)
    full = _fit(image, MAX_EDGE)
    thumb = _fit(full, THUMB_EDGE)
    data = _encode(full, WEBP_QUALITY)
    return Imported(
        data=data,
        thumb=_encode(thumb, THUMB_QUALITY),
        mime=MIME,
        width=full.width,
        height=full.height,
        bytes=len(data),
        sha256=sha256_hex(raw),
        dhash=dhash(image),
    )
