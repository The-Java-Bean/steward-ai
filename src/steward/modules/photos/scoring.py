# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Scoring the `caption` role (`steward eval caption`): the real image preparation,
prompt and cleaning on images drawn here at run time, so the set holds no binaries and
no people. `concepts`: what's in the image is named (each entry of `must` lists words
that would do). `no_invention`: nothing from `never` is claimed. `described`: there's
an answer at all."""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import partial
from math import cos, pi, sin
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from steward.modules.photos import caption, images
from steward.sdk import EvalCaseResult, EvalModel

SIZE = (640, 480)
WHITE = (255, 255, 255)
BLACK = (20, 20, 20)
RED = (205, 30, 30)
BLUE = (40, 80, 200)
YELLOW = (240, 200, 40)
GREEN = (60, 150, 70)
GREY = (120, 120, 120)

type Canvas = ImageDraw.ImageDraw


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _red_circle(d: Canvas) -> None:
    d.ellipse((170, 90, 470, 390), fill=RED)


def _shapes(d: Canvas) -> None:
    d.rectangle((90, 140, 290, 340), fill=BLUE)
    d.polygon([(350, 340), (550, 340), (450, 140)], fill=YELLOW)


def _bar_chart(d: Canvas) -> None:
    d.text((190, 20), "Monthly Sales", fill=BLACK, font=_font(32))
    d.line((80, 420, 600, 420), fill=BLACK, width=3)
    d.line((80, 80, 80, 420), fill=BLACK, width=3)
    for i, height in enumerate((120, 200, 160, 280, 240, 310)):
        x = 110 + i * 80
        d.rectangle((x, 420 - height, x + 50, 420), fill=BLUE)


def _pie_chart(d: Canvas) -> None:
    box = (160, 80, 480, 400)
    d.pieslice(box, 0, 160, fill=RED)
    d.pieslice(box, 160, 280, fill=BLUE)
    d.pieslice(box, 280, 360, fill=YELLOW)


def _document(d: Canvas) -> None:
    d.text((60, 30), "INVOICE", fill=BLACK, font=_font(44))
    d.text((60, 90), "Harbor Lane Hardware", fill=BLACK, font=_font(22))
    for row in range(5):
        y = 150 + row * 44
        d.text((60, y), f"Item {row + 1}", fill=BLACK, font=_font(20))
        d.text((480, y), f"{(row + 2) * 12}.00", fill=BLACK, font=_font(20))
        d.line((60, y + 32, 580, y + 32), fill=GREY)
    d.text((380, 400), "Total 240.00", fill=BLACK, font=_font(26))


def _stop_sign(d: Canvas) -> None:
    d.rectangle((0, 0, *SIZE), fill=(150, 200, 235))
    d.rectangle((308, 300, 332, 480), fill=GREY)
    center, radius = (320, 190), 150
    points = [
        (
            center[0] + radius * cos(pi / 8 + i * pi / 4),
            center[1] + radius * sin(pi / 8 + i * pi / 4),
        )
        for i in range(8)
    ]
    d.polygon(points, fill=RED)
    d.text((212, 150), "STOP", fill=WHITE, font=_font(80))


def _sunset(d: Canvas) -> None:
    for y in range(300):
        d.line((0, y, 640, y), fill=(250, 150 - y // 4, 60 + y // 6))
    d.ellipse((250, 200, 390, 340), fill=(255, 225, 120))
    d.rectangle((0, 300, 640, 480), fill=(30, 50, 90))
    for y in (330, 370, 420):
        d.line((180, y, 460, y), fill=(240, 170, 90), width=3)


def _night(d: Canvas) -> None:
    d.rectangle((0, 0, *SIZE), fill=(12, 16, 50))
    d.ellipse((460, 50, 550, 140), fill=(240, 240, 215))
    for x, y in ((60, 60), (140, 130), (230, 50), (320, 110), (400, 40), (580, 200), (90, 220)):
        d.ellipse((x, y, x + 5, y + 5), fill=WHITE)
    d.polygon(
        [(0, 480), (0, 340), (180, 240), (340, 350), (500, 260), (640, 340), (640, 480)],
        fill=(8, 10, 24),
    )


def _screenshot(d: Canvas) -> None:
    d.rectangle((0, 0, *SIZE), fill=(236, 238, 242))
    d.rectangle((0, 0, 640, 44), fill=(60, 64, 76))
    for i, color in enumerate((RED, YELLOW, GREEN)):
        d.ellipse((14 + i * 24, 14, 30 + i * 24, 30), fill=color)
    d.text((270, 10), "Settings", fill=WHITE, font=_font(22))
    d.rectangle((0, 44, 170, 480), fill=(214, 217, 224))
    for i, label in enumerate(("General", "Network", "Privacy", "Backups")):
        d.text((20, 70 + i * 44), label, fill=BLACK, font=_font(20))
    for i in range(4):
        y = 80 + i * 70
        d.text((200, y), f"Option {i + 1}", fill=BLACK, font=_font(20))
        d.rounded_rectangle((540, y, 600, y + 28), 14, fill=BLUE if i % 2 else GREY)


def _house(d: Canvas) -> None:
    d.rectangle((0, 0, 640, 340), fill=(170, 215, 245))
    d.rectangle((0, 340, 640, 480), fill=GREEN)
    d.ellipse((510, 30, 590, 110), fill=YELLOW)
    d.rectangle((180, 210, 400, 390), fill=(225, 205, 170))
    d.polygon([(150, 215), (430, 215), (290, 100)], fill=(160, 50, 40))
    d.rectangle((265, 300, 320, 390), fill=(110, 70, 40))
    d.rectangle((205, 245, 255, 295), fill=(190, 225, 245))
    d.rectangle((335, 245, 385, 295), fill=(190, 225, 245))
    d.rectangle((70, 290, 96, 390), fill=(110, 70, 40))
    d.ellipse((20, 190, 146, 316), fill=(40, 120, 55))


def _checkerboard(d: Canvas) -> None:
    for row in range(6):
        for column in range(8):
            if (row + column) % 2 == 0:
                d.rectangle((column * 80, row * 80, column * 80 + 79, row * 80 + 79), fill=BLACK)


def _open_sign(d: Canvas) -> None:
    d.rectangle((0, 0, *SIZE), fill=(50, 40, 40))
    d.rounded_rectangle((110, 130, 530, 350), 24, fill=(20, 110, 60), outline=WHITE, width=6)
    d.text((170, 165), "OPEN", fill=WHITE, font=_font(120))


DRAWINGS: Mapping[str, Callable[[Canvas], None]] = {
    "red_circle": _red_circle,
    "shapes": _shapes,
    "bar_chart": _bar_chart,
    "pie_chart": _pie_chart,
    "document": _document,
    "stop_sign": _stop_sign,
    "sunset": _sunset,
    "night": _night,
    "screenshot": _screenshot,
    "house": _house,
    "checkerboard": _checkerboard,
    "open_sign": _open_sign,
}


def draw(name: str) -> bytes:
    image = Image.new("RGB", SIZE, WHITE)
    DRAWINGS[name](ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _has(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.I) is not None


def score(
    text: str, must: Sequence[Sequence[str]], never: Sequence[str]
) -> tuple[dict[str, float], list[str]]:
    """(metrics, what's wrong)."""
    missing = [group[0] for group in must if not any(_has(w, text) for w in group)]
    invented = [word for word in never if _has(word, text)]
    metrics = {
        "described": 1.0 if text else 0.0,
        "concepts": (len(must) - len(missing)) / len(must) if must else 1.0,
        "no_invention": 0.0 if invented else 1.0,
    }
    return metrics, [*(f"missing {w}" for w in missing), *(f"invented {w}" for w in invented)]


def run(
    data: Mapping[str, Any],
    model: EvalModel,
    *,
    max_side: int,
    max_tokens: int,
    max_chars: int,
) -> Iterator[EvalCaseResult]:
    prepare = partial(images.prepare, max_side=max_side, min_side=16, max_pixels=10_000_000)
    for case in data["cases"]:
        jpeg = prepare(io.BytesIO(draw(case["draw"])))
        answer = model.chat(
            caption.messages(jpeg), options={"temperature": 0, "max_tokens": max_tokens}
        )
        text = caption.clean(answer, max_chars)
        metrics, wrong = score(text, case["must"], case.get("never", ()))
        yield EvalCaseResult(case["id"], metrics, not wrong, "; ".join(wrong))
