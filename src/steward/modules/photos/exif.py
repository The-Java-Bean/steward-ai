# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Photo and video metadata with the exiftool program (docs/adr/0024).

`read_metadata` runs in the stage's isolated child process with the file as stdin and
returns exiftool's JSON record; `normalize` turns it into the properties steward-ai
stores (`taken`, `camera`, `width`…). exiftool is a separate program (Perl), so a hostile
file can at worst crash or hang it; the child's limits and the timeout catch both.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any, BinaryIO

from steward.sdk import PropertyValue

VERSION = 1
"""Raise when `normalize` or the tag list changes (folded into the stage version)."""

TAGS = (
    "-DateTimeOriginal",
    "-OffsetTimeOriginal",
    "-CreationDate",
    "-CreateDate",
    "-DateCreated",
    "-Make",
    "-Model",
    "-LensModel",
    "-Lens",
    "-ImageWidth",
    "-ImageHeight",
    "-Orientation",
    "-Rotation",
    "-Composite:GPSLatitude",
    "-Composite:GPSLongitude",
    "-Composite:GPSAltitude",
    "-Duration",
)
ARGS = ("-json", "-n", "-G0", "-api", "LargeFileSupport=1", *TAGS)
MAX_OUTPUT = 1024 * 1024
STDIN_PATH = "/dev/stdin"

_DATE = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_OFFSET = re.compile(r"^[+-]\d{2}:?\d{2}$")


class ExifFailed(Exception):
    """exiftool didn't give a usable answer (no message: it could hold file content)."""


class ExifTimedOut(Exception):
    """exiftool took longer than its time limit (it was stopped)."""


def environment() -> dict[str, str]:
    return {"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def read_metadata(file: BinaryIO, *, command: str, timeout_s: float) -> dict[str, Any]:
    """exiftool's record for `file`. Runs in the child. The file is exiftool's stdin,
    named by the path `STDIN_PATH` rather than `-`: exiftool reads `-` into memory to
    seek in it (a large video would need gigabytes), while the path opens the same,
    already-checked file again, seekable."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed arguments, the program is configured
            [command, *ARGS, STDIN_PATH],
            stdin=file,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment(),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ExifTimedOut from None
    if len(proc.stdout) > MAX_OUTPUT:
        raise ExifFailed
    if not proc.stdout.strip():
        if proc.returncode == 0:
            return {}
        raise ExifFailed
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise ExifFailed from None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ExifFailed
    return data[0]


def _first(record: Mapping[str, Any], *names: str) -> Any:
    """The first value present for any of `names` (`Make` matches `EXIF:Make`…)."""
    for name in names:
        for key, value in record.items():
            if (key == name or key.endswith(":" + name)) and value not in (None, ""):
                return value
    return None


def _group_of(record: Mapping[str, Any], name: str) -> str | None:
    for key, value in record.items():
        if key.endswith(":" + name) and value not in (None, ""):
            return key.split(":", 1)[0]
    return None


def _offset(value: Any) -> str | None:
    if isinstance(value, str) and _OFFSET.match(value.strip()):
        v = value.strip()
        return v if ":" in v else f"{v[:3]}:{v[3:]}"
    return None


def parse_date(value: Any, *, offset: str | None = None, utc: bool = False) -> str | None:
    """`2024:07:04 15:30:00` (+ an offset) → `2024-07-04T15:30:00+02:00`. None for empty
    or impossible dates (cameras write `0000:00:00 00:00:00`)."""
    if not isinstance(value, str):
        return None
    m = _DATE.match(value.strip())
    if m is None:
        return None
    year, month, day, hour, minute, second, tz = m.groups()
    if not (
        1800 <= int(year) <= 2200
        and 1 <= int(month) <= 12
        and 1 <= int(day) <= 31
        and int(hour) <= 23
        and int(minute) <= 59
        and int(second) <= 60
    ):
        return None
    text = f"{year}-{month}-{day}T{hour}:{minute}:{second}"
    zone = ("Z" if tz == "Z" else _offset(tz)) if tz else offset or ("Z" if utc else None)
    return text + (zone or "")


def _taken(record: Mapping[str, Any]) -> str | None:
    found = parse_date(
        _first(record, "DateTimeOriginal"), offset=_offset(_first(record, "OffsetTimeOriginal"))
    )
    if found:
        return found
    found = parse_date(_first(record, "CreationDate"))  # QuickTime Keys: local time + offset
    if found:
        return found
    group = _group_of(record, "CreateDate")
    found = parse_date(_first(record, "CreateDate"), utc=group == "QuickTime")
    return found or parse_date(_first(record, "DateCreated"))


def _text(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = int(value)
    return number if low <= number <= high and number == value else None


def _float(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if low <= number <= high else None


def _camera(record: Mapping[str, Any]) -> str | None:
    make = _text(_first(record, "Make"), 100)
    model = _text(_first(record, "Model"), 100)
    if make and model:
        return model if model.lower().startswith(make.split()[0].lower()) else f"{make} {model}"
    return model or make


def normalize(record: Mapping[str, Any]) -> dict[str, PropertyValue | None]:
    """The properties stored for a file (absent values are None)."""
    latitude = _float(_first(record, "GPSLatitude"), -90.0, 90.0)
    longitude = _float(_first(record, "GPSLongitude"), -180.0, 180.0)
    if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
        latitude = longitude = None
    duration = _float(_first(record, "Duration"), 0.0, 1e7)
    rotation = _int(_first(record, "Rotation"), 0, 359)
    return {
        "taken": _taken(record),
        "camera": _camera(record),
        "lens": _text(_first(record, "LensModel", "Lens")),
        "width": _int(_first(record, "ImageWidth"), 1, 1_000_000),
        "height": _int(_first(record, "ImageHeight"), 1, 1_000_000),
        "orientation": _int(_first(record, "Orientation"), 1, 8),
        "rotation": rotation or None,
        "latitude": round(latitude, 7) if latitude is not None else None,
        "longitude": round(longitude, 7) if longitude is not None else None,
        "altitude": (
            round(alt, 1)
            if latitude is not None
            and (alt := _float(_first(record, "GPSAltitude"), -1e5, 1e6)) is not None
            else None
        ),
        "duration_s": round(duration, 3) if duration else None,
    }
