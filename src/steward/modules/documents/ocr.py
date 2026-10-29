# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""OCR with Tesseract: scanned PDF pages (rendered with pypdfium2) and images.

`ocr_pdf` and `ocr_image` run in the isolated child (`StageContext.run_isolated`). They
read only the file they're given and start the `tesseract` program, which inherits the
child's memory and CPU limits and is killed with it. Images reach `tesseract` over
stdin, so nothing is written to disk. Like the extractors, failures leave the child as
exception type names only.

No searchable PDF is written: steward-ai never modifies originals, it only needs the
text (docs/adr/0022-ocr-and-throughput.md).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import BinaryIO

from steward.modules.documents.extractors import EncryptedDocument

VERSION = 1
"""Raise when OCR output changes; it's part of `documents.ocr`'s version."""

PDF_MIME = "application/pdf"
IMAGE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/bmp",
        "image/x-ms-bmp",
        "image/gif",
        "image/webp",
    }
)
MAX_PAGE_PIXELS = 40_000_000
"""Rendered pages are scaled down below this (a 300 dpi A4 page is about 8.7 million)."""
PROBE_TIMEOUT_S = 10.0
_LANGUAGE = re.compile(r"^[A-Za-z0-9_]+$")
_BLANK_LINES = re.compile(r"\n{3,}")


class OcrFailed(Exception):
    """`tesseract` exited with an error (its output isn't kept: it can quote the file)."""


@dataclass(frozen=True, slots=True)
class Settings:
    command: str
    """Absolute path of the `tesseract` program."""
    languages: tuple[str, ...]
    dpi: int = 300
    min_chars: int = 20
    """Pages with fewer non-space characters of extracted text are OCR'd (if they hold an image)."""
    max_pages: int = 500
    budget_s: float = 600.0
    """No new page is started after this many seconds (the result says how far it got)."""
    force: bool = False
    """OCR every page, not only pages without enough text."""


@dataclass(frozen=True, slots=True)
class PdfOcr:
    pages: int
    """Pages in the document."""
    text: dict[int, str] = field(default_factory=dict)
    """1-based page -> OCR text, for every page that was OCR'd."""
    unfinished: int = 0
    """Pages that needed OCR but weren't done (page or time limit)."""


def nonspace(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


# --- in the isolated child -----------------------------------------------------------


def ocr_image(f: BinaryIO, *, settings: Settings) -> str:
    """OCR one image (the first frame of multi-frame GIF/TIFF)."""
    return _tesseract(f.read(), settings, dpi=None)


def ocr_pdf(f: BinaryIO, *, settings: Settings, page_chars: dict[int, int]) -> PdfOcr:
    """OCR the pages of a PDF that need it. `page_chars`: 1-based page -> non-space
    characters of its extracted text."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    try:
        pdf = pdfium.PdfDocument(f.read())
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise EncryptedDocument from None
        raise
    try:
        total = len(pdf)
        todo = [
            n
            for n in range(1, total + 1)
            if settings.force or (page_chars.get(n, 0) < settings.min_chars and _has_image(pdf, n))
        ]
        done: dict[int, str] = {}
        for i, number in enumerate(todo):
            if i >= settings.max_pages or time.monotonic() - started > settings.budget_s:
                return PdfOcr(total, done, unfinished=len(todo) - i)
            done[number] = _ocr_page(pdf, number, settings)
        return PdfOcr(total, done)
    finally:
        pdf.close()


def _has_image(pdf: object, number: int) -> bool:
    import pypdfium2.raw as pdfium_c

    page = pdf[number - 1]  # type: ignore[index]
    try:
        images = page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4)
        return next(iter(images), None) is not None
    finally:
        page.close()


def _ocr_page(pdf: object, number: int, settings: Settings) -> str:
    page = pdf[number - 1]  # type: ignore[index]
    try:
        width, height = page.get_size()
        area = max(width * height, 1.0)
        scale = min(settings.dpi / 72, math.sqrt(MAX_PAGE_PIXELS / area))
        bitmap = page.render(scale=scale, grayscale=True, may_draw_forms=True)
        try:
            image = _pgm(bitmap.buffer, bitmap.width, bitmap.height, bitmap.stride)
        finally:
            bitmap.close()
    finally:
        page.close()
    return _tesseract(image, settings, dpi=round(72 * scale))


def _pgm(buffer: object, width: int, height: int, stride: int) -> bytes:
    """A binary PGM (the simplest image format Tesseract reads) from 8-bit gray rows."""
    data = memoryview(buffer).cast("B")  # type: ignore[arg-type]
    if stride == width:
        rows = bytes(data[: width * height])
    else:
        rows = b"".join(data[r * stride : r * stride + width] for r in range(height))
    return b"P5\n%d %d\n255\n" % (width, height) + rows


def _tesseract(image: bytes, settings: Settings, *, dpi: int | None) -> str:
    cmd = [settings.command, "stdin", "stdout", "-l", "+".join(settings.languages)]
    if dpi is not None:
        cmd += ["--dpi", str(dpi)]
    proc = subprocess.run(  # noqa: S603 -- fixed program, no shell
        cmd, input=image, capture_output=True, env=_env(), check=False
    )
    if proc.returncode != 0:
        raise OcrFailed
    return clean(proc.stdout.decode("utf-8", "replace"))


def clean(text: str) -> str:
    text = text.replace("\f", "\n").replace("\r\n", "\n")
    lines = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", lines).strip()


def _env() -> dict[str, str]:
    """One thread per page (the `cpu` lane decides parallelism), no locale surprises."""
    env = {"OMP_THREAD_LIMIT": "1", "LC_ALL": "C", "PATH": os.environ.get("PATH", "")}
    if "TESSDATA_PREFIX" in os.environ:
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
    return env


# --- in the worker and the API (probing the installed program) ----------------------


@functools.cache
def installed_languages(command: str) -> frozenset[str]:
    """Languages `tesseract --list-langs` reports (empty if it can't be run)."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed program, no shell
            [command, "--list-langs"],
            capture_output=True,
            env=_env(),
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    lines = proc.stdout.decode("utf-8", "replace").splitlines()[1:]  # after the header
    return frozenset(line.strip() for line in lines if _LANGUAGE.match(line.strip()))


def valid_language(name: str) -> bool:
    return bool(_LANGUAGE.match(name))
