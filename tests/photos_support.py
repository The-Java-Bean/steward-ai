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
import json, os, re, sys, time
here = os.path.dirname(os.path.abspath(__file__))

def load(name):
    found = re.search(rb"<fake>(.*)</fake>", open(name, "rb").read(), re.S)
    return json.loads(found.group(1)) if found else {{}}

def dump(name, record):
    body = json.dumps(record, sort_keys=True).encode()
    with open(name, "wb") as out:
        out.write(b'<?xpacket begin="" ?><x:xmpmeta><fake>' + body + b"</fake></x:xmpmeta>")

def edit(name, args):
    if b"NOEDIT" in open(name, "rb").read():
        return
    record = load(name)
    for arg in args:
        tag, op, value = re.match(r"-XMP-[a-z]+:(\\w+?)(\\+=|-=|=)(.*)", arg).groups()
        if op == "=":
            record[tag] = value or None
            continue
        values = record.setdefault(tag, [])
        if value in values:
            values.remove(value)
        if op == "+=":
            values.append(value)
    dump(name, record)

argv = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"args": argv, "env": sorted(os.environ)}}) + "\\n")
if os.path.exists(os.path.join(here, "BROKEN")):
    sys.exit(1)
if "-@" in argv:
    pending = []
    for line in open(argv[argv.index("-@") + 1], encoding="utf-8").read().splitlines():
        if line == "-execute":
            edit(pending[-1], pending[:-1])
            pending = []
        else:
            pending.append(line)
    sys.exit(0)
names = [a for a in argv if a.endswith(".xmp")]
if names:
    out = []
    for name in names:
        record = load(name)
        kept = {{k: v for k, v in record.items() if v not in (None, [])}}
        out.append({{"SourceFile": name, **kept}})
        if b"UNREADABLE" in open(name, "rb").read():
            out[-1]["Error"] = "bad"
    print(json.dumps(out))
    sys.exit(0)
data = sys.stdin.buffer.read()
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


def fake_sidecar(**record: Any) -> bytes:
    """A sidecar as the fake exiftool keeps them: `Subject=[...]`, `Description="..."`..."""
    body = json.dumps(record, sort_keys=True).encode()
    return b'<?xpacket begin="" ?><x:xmpmeta><fake>' + body + b"</fake></x:xmpmeta>"


def read_fake_sidecar(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return dict(json.loads(data[data.index(b"<fake>") + 6 : data.index(b"</fake>")]))


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
