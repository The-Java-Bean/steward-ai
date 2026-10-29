# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Splitting text into retrieval chunks.

Chunks never cross a page. Consecutive short sections on the same page are packed
together (the chunk keeps the first section's heading); long ones are split at
paragraph, then sentence, then word boundaries, so a chunk is at most `size`
characters, and each chunk after the first in a run starts with up to `overlap`
characters from the end of the previous one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from steward.sdk import ChunkPart, TextPart

_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
_SENTENCE = re.compile(r"(?<=[.!?…。])\s+")
_TRAILING = re.compile(r"[ \t]+\n")
_BLANKS = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING.sub("\n", text)
    return _BLANKS.sub("\n\n", text).strip()


def _units(text: str, size: int) -> list[str]:
    """Pieces of at most `size` characters, in order."""
    units: list[str] = []
    for para in _PARAGRAPH.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            units.append(para)
            continue
        for sentence in _SENTENCE.split(para):
            if len(sentence) <= size:
                units.append(sentence)
                continue
            units.extend(_by_words(sentence, size))
    return units


def _by_words(text: str, size: int) -> Iterable[str]:
    current = ""
    for word in text.split():
        while len(word) > size:  # one enormous "word": cut it
            if current:
                yield current
                current = ""
            yield word[:size]
            word = word[size:]
        if current and len(current) + 1 + len(word) > size:
            yield current
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        yield current


def _tail(text: str, overlap: int) -> str:
    """The last `overlap` characters, starting at a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    cut = text[-overlap:]
    space = cut.find(" ")
    return cut[space + 1 :] if 0 <= space < len(cut) - 1 else cut


def _pack(units: Sequence[str], size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        joined = f"{current}\n\n{unit}" if current else unit
        if len(joined) <= size:
            current = joined
            continue
        chunks.append(current)
        carry = _tail(current, overlap)
        current = f"{carry} {unit}" if carry and len(carry) + 1 + len(unit) <= size else unit
    if current:
        chunks.append(current)
    return chunks


def chunk_parts(parts: Sequence[TextPart], *, size: int, overlap: int) -> list[ChunkPart]:
    if size < 100 or not 0 <= overlap < size // 2:
        raise ValueError("chunk size must be >= 100 and overlap below half of it")
    out: list[ChunkPart] = []
    group: list[TextPart] = []

    def flush() -> None:
        if not group:
            return
        text = "\n\n".join(normalize(p.body) for p in group if p.body.strip())
        heading = next((p.heading for p in group if p.heading), None)
        out.extend(
            ChunkPart(body, page=group[0].page, heading=heading)
            for body in _pack(_units(text, size), size, overlap)
        )
        group.clear()

    length = 0
    for part in parts:
        body = normalize(part.body)
        if not body:
            continue
        same_page = not group or group[0].page == part.page
        if not same_page or (group and length + 2 + len(body) > size):
            flush()
            length = 0
        group.append(part)
        length += len(body) + 2
    flush()
    return out
