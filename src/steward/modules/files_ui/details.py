# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Turning a file's properties into labelled lines for the file page."""

from __future__ import annotations

from collections.abc import Mapping

from steward.sdk import FileDetails, PropertyValue

LABELS = {
    "taken": "Taken",
    "camera": "Camera",
    "lens": "Lens",
    "duration_s": "Duration",
    "altitude": "Altitude",
}
SKIPPED = frozenset({"width", "height", "latitude", "longitude", "orientation", "rotation"})
"""Shown combined (size, location) or not at all."""


def _duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _taken(value: str) -> str:
    date, _, time = value.partition("T")
    if not time:
        return date
    zone = ""
    if time.endswith("Z"):
        time, zone = time[:-1], " UTC"
    elif len(time) > 8 and time[8] in "+-":
        time, zone = time[:8], f" (UTC{time[8:]})"
    return f"{date} {time[:5]}{zone}"


def _value(key: str, value: PropertyValue) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "taken" and isinstance(value, str):
        return _taken(value)
    if key == "duration_s" and isinstance(value, int | float):
        return _duration(float(value))
    if key == "altitude" and isinstance(value, int | float):
        return f"{value:g} m"
    return str(value)


def _label(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").capitalize()


def detail_rows(details: FileDetails) -> list[tuple[str, str]]:
    """(label, value) lines: size and location combined, then each other property."""
    rows: list[tuple[str, str]] = []
    for props in details.properties.values():
        rows.extend(_rows(props))
    return rows


def _rows(props: Mapping[str, PropertyValue]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if "taken" in props:
        rows.append((_label("taken"), _value("taken", props["taken"])))
    width, height = props.get("width"), props.get("height")
    if width and height:
        rows.append(("Size", f"{width} x {height} pixels"))
    lat, lon = props.get("latitude"), props.get("longitude")
    if isinstance(lat, int | float) and isinstance(lon, int | float):
        rows.append(("Location", f"{lat:.5f}, {lon:.5f}"))
    for key, value in props.items():
        if key == "taken" or key in SKIPPED:
            continue
        rows.append((_label(key), _value(key, value)))
    return rows
