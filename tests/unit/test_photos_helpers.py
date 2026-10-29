# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `photos` module's pure parts: EXIF normalization, image preparation, cleaning
captions, and how the file page shows properties."""

from __future__ import annotations

import base64
import io

import pytest
from photos_support import make_jpeg, make_png
from PIL import Image, UnidentifiedImageError

from steward.modules.files_ui.details import detail_rows
from steward.modules.photos import caption, exif, images
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
