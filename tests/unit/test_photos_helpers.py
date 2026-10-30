# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `photos` module's pure parts: EXIF normalization, image preparation, cleaning
captions, and how the file page shows properties."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from photos_support import make_fake_exiftool, make_jpeg, make_png
from PIL import Image, UnidentifiedImageError

from steward.modules.files_ui.details import detail_rows
from steward.modules.photos import caption, exif, images, raw
from steward.sdk import FileDetails

# --- EXIF ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "kwargs", "expected"),
    [
        ("2024:07:04 15:30:00", {}, "2024-07-04T15:30:00"),
        ("2024:07:04 15:30:00", {"offset": "+02:00"}, "2024-07-04T15:30:00+02:00"),
        ("2024:07:04 15:30:00.123+0530", {}, "2024-07-04T15:30:00+05:30"),
        ("2024:07:04 15:30:00", {"utc": True}, "2024-07-04T15:30:00Z"),
        ("2024:07:04 15:30:00Z", {}, "2024-07-04T15:30:00Z"),
        ("0000:00:00 00:00:00", {}, None),
        ("2024:13:01 00:00:00", {}, None),
        ("yesterday", {}, None),
        (20240704, {}, None),
    ],
)
def test_parse_date(value: object, kwargs: dict[str, object], expected: str | None) -> None:
    assert exif.parse_date(value, **kwargs) == expected  # type: ignore[arg-type]


def test_normalize_a_camera_photo() -> None:
    record = {
        "SourceFile": "-",
        "EXIF:DateTimeOriginal": "2023:05:01 12:00:00",
        "EXIF:OffsetTimeOriginal": "+02:00",
        "EXIF:Make": "Canon",
        "EXIF:Model": "Canon EOS R5",
        "EXIF:LensModel": "RF24-105mm F4 L IS USM",
        "File:ImageWidth": 8192,
        "File:ImageHeight": 5464,
        "EXIF:Orientation": 6,
        "Composite:GPSLatitude": 47.3769123456,
        "Composite:GPSLongitude": -8.5417,
        "Composite:GPSAltitude": 408.26,
    }
    assert exif.normalize(record) == {
        "taken": "2023-05-01T12:00:00+02:00",
        "camera": "Canon EOS R5",
        "lens": "RF24-105mm F4 L IS USM",
        "width": 8192,
        "height": 5464,
        "orientation": 6,
        "rotation": None,
        "latitude": 47.3769123,
        "longitude": -8.5417,
        "altitude": 408.3,
        "duration_s": None,
    }


def test_normalize_a_phone_video() -> None:
    record = {
        "QuickTime:CreateDate": "2022:12:24 18:00:00",
        "QuickTime:Make": "Apple",
        "QuickTime:Model": "iPhone 14",
        "QuickTime:Duration": 12.345678,
        "QuickTime:Rotation": 90,
    }
    found = exif.normalize(record)
    assert found["taken"] == "2022-12-24T18:00:00Z"  # QuickTime dates are UTC
    assert found["camera"] == "Apple iPhone 14"
    assert found["duration_s"] == 12.346
    assert found["rotation"] == 90
    keyed = {**record, "QuickTime:CreationDate": "2022:12:24 19:00:00+01:00"}
    assert exif.normalize(keyed)["taken"] == "2022-12-24T19:00:00+01:00"


def test_normalize_drops_nonsense() -> None:
    found = exif.normalize(
        {
            "Composite:GPSLatitude": 0,
            "Composite:GPSLongitude": 0,
            "Composite:GPSAltitude": 12,
            "EXIF:Orientation": 42,
            "File:ImageWidth": "wide",
            "EXIF:Make": "  ",
            "EXIF:DateTimeOriginal": "0000:00:00 00:00:00",
        }
    )
    assert all(v is None for v in found.values())
    assert (
        exif.normalize({"Composite:GPSLatitude": 91, "Composite:GPSLongitude": 10})["latitude"]
        is None
    )


# --- images --------------------------------------------------------------------------


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_prepare_scales_down_and_applies_the_orientation() -> None:
    data = make_jpeg((2000, 1000), orientation=6)  # rotated 90° on display
    out = images.prepare(io.BytesIO(data), max_side=512, min_side=100, max_pixels=10**8)
    with _open(out) as image:
        assert image.format == "JPEG"
        assert image.size == (256, 512)


def test_prepare_flattens_transparency() -> None:
    out = images.prepare(
        io.BytesIO(make_png(alpha=True)), max_side=512, min_side=100, max_pixels=10**8
    )
    with _open(out) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == pytest.approx((255, 255, 255), abs=2)


def test_prepare_refuses_small_huge_and_unknown_images() -> None:
    with pytest.raises(images.TooSmall):
        images.prepare(io.BytesIO(make_png((64, 64))), max_side=512, min_side=100, max_pixels=10**8)
    with pytest.raises(images.TooManyPixels):
        images.prepare(
            io.BytesIO(make_png((400, 300))), max_side=512, min_side=100, max_pixels=1000
        )
    with pytest.raises(UnidentifiedImageError):
        images.prepare(io.BytesIO(b"%PDF-1.7\n"), max_side=512, min_side=100, max_pixels=10**8)


# --- captions ------------------------------------------------------------------------


def test_messages_carry_the_prompt_and_the_image() -> None:
    sent = caption.messages(b"\xff\xd8jpeg")
    assert sent[0]["role"] == "system"
    assert "Don't guess who people are" in sent[0]["content"]
    parts = sent[1]["content"]
    assert parts[0] == {"type": "text", "text": caption.REQUEST}
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\xff\xd8jpeg"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("A dog on a beach.", "A dog on a beach."),
        ("<think>hmm, a dog?</think>\n\nA dog on a beach.", "A dog on a beach."),
        ("cut reasoning</think> A dog.", "A dog."),
        ('Caption: "A **red** barn in snow."', "A red barn in snow."),
        ("“A cat.”", "A cat."),
        ("   ", ""),
    ],
)
def test_clean(answer: str, expected: str) -> None:
    assert caption.clean(answer, 1000) == expected


def test_clean_cuts_long_answers_at_a_sentence() -> None:
    answer = "First sentence here. " * 10 + "A long tail " * 50
    out = caption.clean(answer, 200)
    assert len(out) <= 200
    assert out.endswith(".")
    assert caption.clean("word " * 100, 100).endswith(" …")


# --- file page -----------------------------------------------------------------------


def test_detail_rows_combine_and_label() -> None:
    details = FileDetails(
        description="A dog.",
        properties={
            "exif": {
                "taken": "2023-05-01T12:00:00+02:00",
                "camera": "Canon EOS R5",
                "width": 800,
                "height": 600,
                "latitude": 47.37691,
                "longitude": -8.5417,
                "orientation": 6,
                "duration_s": 3725.2,
                "altitude": 408.3,
            },
            "fields": {"vendor_name": "ACME", "paid": True},
        },
    )
    assert detail_rows(details) == [
        ("Taken", "2023-05-01 12:00 (UTC+02:00)"),
        ("Size", "800 x 600 pixels"),
        ("Location", "47.37691, -8.54170"),
        ("Camera", "Canon EOS R5"),
        ("Duration", "1:02:05"),
        ("Altitude", "408.3 m"),
        ("Vendor name", "ACME"),
        ("Paid", "yes"),
    ]
    utc = FileDetails(properties={"exif": {"taken": "2022-12-24T18:00:00Z"}})
    assert detail_rows(utc) == [("Taken", "2022-12-24 18:00 UTC")]


# --- RAW previews ---------------------------------------------------------------------


def _raw(tmp_path: Path, record: Mapping[str, object], *, tail: bytes = b"") -> tuple[Path, str]:
    """A fake RAW file and the fake exiftool that answers with `record` for it."""
    path = tmp_path / "shot.cr3"
    path.write_bytes(b"EXIF:" + json.dumps(record).encode() + b"\n" + tail)
    return path, str(make_fake_exiftool(tmp_path / "bin"))


def _b64(data: bytes) -> str:
    return "base64:" + base64.b64encode(data).decode()


def _prepare(path: Path, command: str, *, min_side: int = 64, timeout_s: float = 10) -> Image.Image:
    with path.open("rb") as f:
        jpeg = raw.prepare(
            f,
            command=command,
            timeout_s=timeout_s,
            max_side=512,
            min_side=min_side,
            max_pixels=10_000_000,
        )
    return Image.open(io.BytesIO(jpeg))


def test_a_raw_file_gives_its_preview(tmp_path: Path) -> None:
    path, command = _raw(tmp_path, {"PreviewImage": _b64(make_jpeg((600, 400)))})
    assert _prepare(path, command).size == (512, 341)


def test_a_preview_is_turned_by_the_raw_files_orientation(tmp_path: Path) -> None:
    record = {"Orientation": 6, "PreviewImage": _b64(make_jpeg((600, 400)))}
    path, command = _raw(tmp_path, record)
    assert _prepare(path, command).size == (341, 512)


def test_a_previews_own_orientation_isnt_applied_twice(tmp_path: Path) -> None:
    record = {"Orientation": 6, "PreviewImage": _b64(make_jpeg((600, 400), orientation=1))}
    path, command = _raw(tmp_path, record)
    assert _prepare(path, command).size == (512, 341)


def test_a_preview_too_small_gives_way_to_the_next(tmp_path: Path) -> None:
    record = {
        "PreviewImage": _b64(make_jpeg((80, 60))),
        "JpgFromRaw": _b64(make_jpeg((800, 600))),
    }
    path, command = _raw(tmp_path, record)
    assert _prepare(path, command, min_side=256).size == (512, 384)


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"Orientation": 6},
        {"PreviewImage": _b64(make_jpeg((80, 60)))},
        {"PreviewImage": _b64(b"not an image")},
        {"PreviewImage": "base64:***"},
        {"PreviewImage": "(Binary data 100 bytes)"},
        {"PreviewImage": _b64(make_png((600, 400)))},
    ],
)
def test_a_raw_file_without_a_usable_preview(tmp_path: Path, record: dict[str, object]) -> None:
    path, command = _raw(tmp_path, record)
    with pytest.raises(raw.NoPreview):
        _prepare(path, command, min_side=256)


def test_exiftool_failing_means_no_preview(tmp_path: Path) -> None:
    path, command = _raw(tmp_path, {}, tail=b"FAIL")
    with pytest.raises(raw.NoPreview):
        _prepare(path, command)


def test_a_hanging_exiftool_is_stopped_for_a_preview(tmp_path: Path) -> None:
    path, command = _raw(tmp_path, {}, tail=b"SLEEP")
    with pytest.raises(exif.ExifTimedOut):
        _prepare(path, command, timeout_s=0.5)
