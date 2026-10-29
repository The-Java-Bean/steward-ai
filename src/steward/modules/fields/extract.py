# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The fields prompt, the answer's schema and cleaning the values.

Prompts live in `prompts/fields/fields.v<N>.md`; `PROMPT_VERSION` picks the one in use.
Raising it changes the stage version, so every document is read again.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

PROMPT_VERSION = 1
PROMPT_NAME = f"fields.v{PROMPT_VERSION}"

DOC_TYPES = (
    "invoice",
    "receipt",
    "bill",
    "statement",
    "letter",
    "contract",
    "form",
    "report",
    "manual",
    "certificate",
    "ticket",
    "note",
    "other",
)
DEFAULT_FIELDS: Mapping[str, tuple[type, str]] = {
    "doc_type": (str, "One of: " + ", ".join(DOC_TYPES)),
    "title": (str, "A short title, at most ten words"),
    "date": (str, "The document's date as YYYY-MM-DD (or YYYY-MM, YYYY)"),
    "sender": (str, "Who issued or sent it"),
    "recipient": (str, "Who it is addressed to"),
    "amount": (float, "The total amount of money, as a number"),
    "currency": (str, "The amount's currency as a three-letter code"),
}
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EMPTY = frozenset({"", "null", "none", "n/a", "na", "unknown", "-", "not stated"})
CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_DATE = re.compile(r"^(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?$")
MAX_AMOUNT = 1e12


@cache
def prompt() -> str:
    path = resources.files(__package__).joinpath("prompts", "fields", f"{PROMPT_NAME}.md")
    return path.read_text(encoding="utf-8").strip()


def field_names(extra: Sequence[str]) -> tuple[str, ...]:
    """The default fields, then the folder type's (valid names only, no repeats)."""
    names = list(DEFAULT_FIELDS)
    names.extend(n for n in extra if FIELD_NAME.match(n) and n not in names)
    return tuple(dict.fromkeys(names))


@cache
def schema(names: tuple[str, ...]) -> type[BaseModel]:
    """The answer's model: every field nullable."""
    fields: dict[str, Any] = {}
    for name in names:
        kind, description = DEFAULT_FIELDS.get(
            name, (str, f"The document's {name.replace('_', ' ')}")
        )
        fields[name] = (kind | None, Field(None, description=description))
    return create_model("DocumentFields", __config__=ConfigDict(extra="ignore"), **fields)


def messages(file_name: str, text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": prompt()},
        {
            "role": "user",
            "content": f"File name: {file_name}\n\nText:\n<<<\n{text}\n>>>",
        },
    ]


def _text(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if text.casefold() in EMPTY:
        return None
    return text[:max_chars].rstrip() or None


def _date(value: str | None) -> str | None:
    if value is None:
        return None
    m = _DATE.match(value.strip())
    if m is None:
        return None
    year, month, day = (int(g) if g else None for g in m.groups())
    try:
        date(year or 0, month or 1, day or 1)
    except ValueError:
        return None
    if not 1900 <= (year or 0) <= 2200:
        return None
    if month is None:
        return f"{year:04}"
    return f"{year:04}-{month:02}" if day is None else f"{year:04}-{month:02}-{day:02}"


def clean(answer: BaseModel, max_chars: int) -> dict[str, str | float | None]:
    """The values as stored: unknown ones None, dates normalized, amounts finite."""
    values: dict[str, str | float | None] = {}
    for name, raw in answer.model_dump().items():
        if name == "amount":
            ok = isinstance(raw, int | float) and math.isfinite(raw) and abs(raw) <= MAX_AMOUNT
            values[name] = float(raw) if ok else None
            continue
        text = _text(raw, max_chars)
        if name == "date":
            text = _date(text)
        elif name == "doc_type" and text is not None:
            text = text.casefold()
            text = text if text in DOC_TYPES else "other"
        elif name == "currency" and text is not None:
            text = CURRENCY_SIGNS.get(text, text.upper())
            text = text if re.fullmatch(r"[A-Z]{3}", text) else None
        values[name] = text
    return values
