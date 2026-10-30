# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""RAW photos: the JPEG the camera stored inside them (docs/adr/0030).

Decoding RAW would take a large dependency and is far more than a description needs.
Nearly every camera embeds a JPEG preview, which exiftool (already used for metadata) can
hand over. It runs in the stage's isolated child process with the file as stdin, as in
`exif.py`, and asks for all preview tags at once, along with the RAW's orientation:
embedded previews usually don't carry their own.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import subprocess
from typing import BinaryIO

from PIL import UnidentifiedImageError

from steward.modules.photos import exif, images

MIME_TYPES = frozenset(
    {
        "image/x-adobe-dng",
        "image/x-canon-cr2",
        "image/x-canon-cr3",
        "image/x-fuji-raf",
        "image/x-nikon-nef",
        "image/x-olympus-orf",
        "image/x-panasonic-rw2",
        "image/x-pentax-pef",
        "image/x-samsung-srw",
        "image/x-sony-arw",
    }
)
PREVIEW_TAGS = ("PreviewImage", "JpgFromRaw", "OtherImage")
"""Smallest first, in the usual case: any of them is well above what a model is sent."""
ARGS = (
    "-json",
    "-b",
    "-n",
    "-api",
    "LargeFileSupport=1",
    "-Orientation",
    *(f"-{tag}" for tag in PREVIEW_TAGS),
)
MAX_OUTPUT = 256 * 1024 * 1024
BASE64 = "base64:"


class NoPreview(Exception):
    """The file holds no JPEG large enough to describe."""


def _record(file: BinaryIO, command: str, timeout_s: float) -> dict[str, object]:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed arguments, the program is configured
            [command, *ARGS, exif.STDIN_PATH],
            stdin=file,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=exif.environment(),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise exif.ExifTimedOut from None
    if len(proc.stdout) > MAX_OUTPUT:
        raise exif.ExifFailed
    if not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise exif.ExifFailed from None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise exif.ExifFailed
    return data[0]


def _decoded(value: object) -> bytes | None:
    if not isinstance(value, str) or not value.startswith(BASE64):
        return None
    try:
        return base64.b64decode(value[len(BASE64) :], validate=True)
    except binascii.Error:
        return None


def prepare(
    file: BinaryIO,
    *,
    command: str,
    timeout_s: float,
    max_side: int,
    min_side: int,
    max_pixels: int,
) -> bytes:
    """A RAW file's first usable preview, ready for the model (see `images.prepare`).
    Raises `NoPreview` if it has none."""
    record = _record(file, command, timeout_s)
    orientation = record.get("Orientation")
    for tag in PREVIEW_TAGS:
        preview = _decoded(record.get(tag))
        if preview is None:
            continue
        try:
            return images.prepare(
                io.BytesIO(preview),
                max_side=max_side,
                min_side=min_side,
                max_pixels=max_pixels,
                formats=("JPEG",),
                orientation=orientation if isinstance(orientation, int) else None,
            )
        except (images.TooSmall, UnidentifiedImageError, OSError):
            continue
    raise NoPreview
