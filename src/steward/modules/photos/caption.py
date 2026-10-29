# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The caption prompt and cleaning the model's answer.

Prompts live in `prompts/caption/caption.v<N>.md`; `PROMPT_VERSION` picks the one in
use. Raising it changes the stage version, so every photo is described again.
"""

from __future__ import annotations

import base64
import re
from functools import cache
from importlib import resources
from typing import Any

PROMPT_VERSION = 1
PROMPT_NAME = f"caption.v{PROMPT_VERSION}"
REQUEST = "Describe this image."

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_LEAD = re.compile(r"^(?:caption|description)\s*:\s*", re.I)
_QUOTES = "\"'`\u2018\u2019\u201c\u201d"


@cache
def prompt() -> str:
    path = resources.files(__package__).joinpath("prompts", "caption", f"{PROMPT_NAME}.md")
    return path.read_text(encoding="utf-8").strip()


def messages(jpeg: bytes) -> list[dict[str, Any]]:
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return [
        {"role": "system", "content": prompt()},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": REQUEST},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        },
    ]


def clean(answer: str, max_chars: int) -> str:
    """The description as stored: no reasoning blocks, labels, wrapping quotes or extra
    whitespace; cut at a sentence end before `max_chars` when possible."""
    text = _THINK.sub(" ", answer)
    if "</think>" in text:  # an unclosed reasoning block was cut at the start
        text = text.rsplit("</think>", 1)[1]
    text = " ".join(text.replace("**", "").split())
    text = _LEAD.sub("", text)
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = cut.rfind(". ")
        text = cut[: end + 1] if end >= max_chars // 2 else cut.rsplit(" ", 1)[0] + " …"
    return text
