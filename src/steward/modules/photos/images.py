# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Getting a photo ready for a vision model (runs in the isolated child process).

Pillow decodes only the formats listed in `FORMATS` (no exotic decoders), refuses images
above the pixel limit, applies the EXIF orientation, flattens transparency onto white
and returns a JPEG no larger than `max_side` on its longest side. Large JPEGs are
decoded at a reduced size (draft mode), so a 50-megapixel photo costs little. RAW files
go through `raw.py`, which hands the JPEG embedded in them to `prepare`.
"""

from __future__ import annotations

import io
import warnings
from typing import BinaryIO

from PIL import Image, ImageOps

VERSION = 1
FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP")
MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/tiff", "image/bmp"}
)
JPEG_QUALITY = 85
ORIENTATION_TAG = 0x0112


class TooSmall(Exception):
    """Smaller than the minimum size on its shorter side (icons, thumbnails)."""


class TooManyPixels(Exception):
    """More pixels than the limit."""


def prepare(
    file: BinaryIO,
    *,
    max_side: int,
    min_side: int,
    max_pixels: int,
    formats: tuple[str, ...] = FORMATS,
    orientation: int | None = None,
) -> bytes:
    """The image as a JPEG at most `max_side` pixels wide and high. `orientation` (an EXIF
    value) applies when the image has none of its own."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            image = Image.open(file, formats=formats)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise TooManyPixels from None
        with image:
            width, height = image.size
            if width * height > max_pixels:
                raise TooManyPixels
            if min(width, height) < min_side:
                raise TooSmall
            image.draft("RGB", (max_side, max_side))
            exif = image.getexif()
            if orientation and not exif.get(ORIENTATION_TAG):
                exif[ORIENTATION_TAG] = orientation
            rotated = ImageOps.exif_transpose(image)
            if rotated.mode in ("RGBA", "LA", "PA") or (
                rotated.mode == "P" and "transparency" in rotated.info
            ):
                rgba = rotated.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flat = rotated.convert("RGB")
            flat.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=JPEG_QUALITY)
            return out.getvalue()
