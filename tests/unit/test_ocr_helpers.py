# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import io
from pathlib import Path

import pytest
from doc_fixtures import make_pdf
from ocr_support import SCANNED_WORD, calls, make_fake_tesseract

from steward.modules.documents import merge_pages, ocr
from steward.sdk import FolderInfo, TextPart


def test_merge_puts_ocr_pages_in_place() -> None:
    text = [TextPart("cover", page=1), TextPart(" ", page=2), TextPart("end", page=3)]
    result = ocr.PdfOcr(pages=3, text={2: "scanned middle"})
    assert merge_pages(text, result, 1000) == [
        TextPart("cover", page=1),
        TextPart("scanned middle", page=2),
        TextPart("end", page=3),
    ]
    # Empty OCR output keeps what extraction had; the text limit still applies.
    assert merge_pages(text, ocr.PdfOcr(pages=3, text={2: ""}), 7) == [
        TextPart("cover", page=1),
        TextPart(" ", page=2),
        TextPart("e", page=3),
    ]


def test_clean() -> None:
    assert ocr.clean("line one  \r\n\n\n\nline two\n\f") == "line one\n\nline two"


def test_pgm_skips_row_padding() -> None:
    rows = bytes([1, 2, 9, 9, 3, 4, 9, 9])
    assert ocr._pgm(bytearray(rows), 2, 2, 4) == b"P5\n2 2\n255\n" + bytes([1, 2, 3, 4])


def test_folder_info_adds_by_full_or_short_name() -> None:
    folder = FolderInfo("scans", "full", "normal", False, (), (), frozenset({"ocr"}))
    assert folder.adds("documents.ocr")
    assert not folder.adds("photos.caption")


def test_pdf_pages_needing_ocr(tmp_path: Path) -> None:
    fake = make_fake_tesseract(tmp_path)
    pdf = make_pdf(["typed", "", "", "x"], images={2, 4})
    settings = ocr.Settings(command=str(fake), languages=("eng",), dpi=150)
    # Page 1 has text, 3 is blank without an image, 2 and 4 are scans (4 has a stray "x").
    result = ocr.ocr_pdf(io.BytesIO(pdf), settings=settings, page_chars={1: 5, 4: 1})
    assert result == ocr.PdfOcr(4, {2: SCANNED_WORD, 4: SCANNED_WORD})
    assert [c["args"][-1] for c in calls(fake)] == ["150", "150"]
    forced = ocr.Settings(command=str(fake), languages=("eng",), force=True, max_pages=3)
    result = ocr.ocr_pdf(io.BytesIO(pdf), settings=forced, page_chars={1: 5})
    assert (sorted(result.text), result.unfinished) == ([1, 2, 3], 1)


def test_huge_pages_are_rendered_below_the_pixel_cap(tmp_path: Path) -> None:
    fake = make_fake_tesseract(tmp_path)
    pdf = make_pdf([""], images={1}).replace(b"[0 0 612 792]", b"[0 0 14400 14400]")
    settings = ocr.Settings(command=str(fake), languages=("eng",))
    ocr.ocr_pdf(io.BytesIO(pdf), settings=settings, page_chars={})
    dpi = int(calls(fake)[0]["args"][-1])
    assert (14400 / 72 * dpi) ** 2 <= ocr.MAX_PAGE_PIXELS * 1.05  # dpi is rounded


def test_an_encrypted_pdf(tmp_path: Path) -> None:
    from doc_fixtures import make_encrypted_pdf

    from steward.modules.documents.extractors import EncryptedDocument

    settings = ocr.Settings(command=str(make_fake_tesseract(tmp_path)), languages=("eng",))
    with pytest.raises(EncryptedDocument):
        ocr.ocr_pdf(io.BytesIO(make_encrypted_pdf([""])), settings=settings, page_chars={})


def test_installed_languages(tmp_path: Path) -> None:
    fake = make_fake_tesseract(tmp_path, ("eng", "deu", "chi_sim"))
    assert ocr.installed_languages(str(fake)) == {"eng", "deu", "chi_sim"}
    assert ocr.installed_languages(str(tmp_path / "nope")) == frozenset()
