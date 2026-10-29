# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A stand-in for the `tesseract` program, so OCR tests run without it installed.

It answers `--list-langs` with the languages it was made with, and otherwise reads an
image from stdin and prints text: `SCANNED_WORD` for a rendered PDF page (PGM), the
words after `SAY:` for any other image. `SLEEP` in the image makes it hang (it writes
its pid to `sleeping.pid` first), `FAIL` makes it fail with the image on stderr. Every
call is logged as one JSON line to `calls.jsonl` next to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCANNED_WORD = "zanzibarquill"

_SCRIPT = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if args == ["--list-langs"]:
    print('List of available languages in "/fake/tessdata/" ({count}):')
    for lang in {languages!r}:
        print(lang)
    sys.exit(0)
data = sys.stdin.buffer.read()
call = {{"args": args, "pgm": data.startswith(b"P5"), "env": sorted(os.environ)}}
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps(call) + "\\n")
if b"SLEEP" in data:
    with open(os.path.join(here, "sleeping.pid"), "w") as f:
        f.write(str(os.getpid()))
    time.sleep(60)
if b"FAIL" in data:
    sys.stderr.buffer.write(data)
    sys.exit(1)
if data.startswith(b"P5"):
    print("{word}")
elif b"SAY:" in data:
    print(data.split(b"SAY:", 1)[1].split(b"\\n", 1)[0].decode())
print("\\f", end="")
"""


def make_fake_tesseract(directory: Path, languages: tuple[str, ...] = ("eng", "osd")) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "tesseract"
    path.write_text(
        _SCRIPT.format(
            python=sys.executable, languages=languages, count=len(languages), word=SCANNED_WORD
        )
    )
    path.chmod(0o755)
    return path


def calls(fake: Path) -> list[dict[str, Any]]:
    log = fake.parent / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]
