# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Test helpers for the `photos` module: a stand-in for the `exiftool` program and
sample images made with Pillow.

The fake exiftool reads the file from stdin and answers with the JSON record written
after `EXIF:` in it (up to the end of that line), or a record without tags. `SLEEP` in
the file makes it hang, `FAIL` makes it exit with an error and no output. Every call is
logged as one JSON line to `calls.jsonl` next to it.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image

_SCRIPT = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
data = sys.stdin.buffer.read()
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"args": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
if b"SLEEP" in data:
    time.sleep(60)
if b"FAIL" in data:
    sys.exit(1)
if b"EXIF:" in data:
    record = data.split(b"EXIF:", 1)[1].split(b"\\n", 1)[0].decode()
    print("[" + record + "]")
else:
    print('[{{"SourceFile": "-"}}]')
"""


def make_fake_exiftool(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "exiftool"
    path.write_text(_SCRIPT.format(python=sys.executable))
    path.chmod(0o755)
    return path


def exiftool_calls(fake: Path) -> list[dict[str, Any]]:
    log = fake.parent / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def make_jpeg(
    size: tuple[int, int] = (640, 480),
    *,
    color: tuple[int, int, int] = (40, 120, 200),
    exif: dict[str, Any] | None = None,
    orientation: int | None = None,
) -> bytes:
    """A JPEG; `exif` becomes the fake exiftool's answer (in a JPEG comment)."""
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if exif is not None:
        kwargs["comment"] = ("EXIF:" + json.dumps(exif) + "\n").encode()
    if orientation is not None:
        tags = Image.Exif()
        tags[0x0112] = orientation
        kwargs["exif"] = tags.tobytes()
    image.save(out, "JPEG", **kwargs)
    return out.getvalue()


def make_png(size: tuple[int, int] = (400, 300), *, alpha: bool = False) -> bytes:
    image = Image.new(
        "RGBA" if alpha else "RGB", size, (200, 30, 30, 0) if alpha else (200, 30, 30)
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
