# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `documents` extractors and chunker, without the pipeline around them."""

from __future__ import annotations

import io
import zipfile

import pytest
from doc_fixtures import make_docx, make_encrypted_pdf, make_odt, make_pdf

from steward.modules.documents import chunker, extractors
from steward.modules.documents.extractors import (
    EncryptedDocument,
    Limits,
    NotText,
    UnsafeDocument,
    extract,
    extractor_for,
)
from steward.sdk import TextPart

LIMITS = Limits(max_text_chars=1_000_000, max_pages=100)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ODT = "application/vnd.oasis.opendocument.text"


def run(data: bytes, mime: str, kind: str = "document", limits: Limits = LIMITS) -> list[TextPart]:
    return extract(io.BytesIO(data), mime=mime, kind=kind, limits=limits).parts


# --- plain text -----------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "Grüße aus Köln".encode(),
        "Grüße aus Köln".encode("utf-16"),
        "﻿Grüße aus Köln".encode(),
        "Grüße aus Köln".encode("cp1252"),
    ],
    ids=["utf-8", "utf-16-bom", "utf-8-bom", "cp1252"],
)
def test_plain_text_encodings(data: bytes) -> None:
    assert [p.body for p in run(data, "text/plain", "text")] == ["Grüße aus Köln"]


def test_binary_data_named_like_text_is_not_text() -> None:
    with pytest.raises(NotText):
        run(b"\x00\x01\x02binary", "text/plain", "text")


def test_text_is_cut_at_the_limit() -> None:
    out = extract(
        io.BytesIO(b"a" * 500),
        mime="text/plain",
        kind="text",
        limits=Limits(max_text_chars=100, max_pages=1),
    )
    assert out.truncated
    assert len(out.parts[0].body) == 100


def test_code_and_other_text_kinds_use_the_plain_extractor() -> None:
    assert extractor_for("text/x-python", "text") is extractors.extract_plain
    assert extractor_for("application/json", "text") is extractors.extract_plain
    assert extractor_for("image/png", "image") is None
    assert extractor_for("application/zip", "archive") is None


# --- Markdown and HTML -------------------------------------------------------------


def test_markdown_sections_follow_headings_and_drop_frontmatter() -> None:
    md = (
        "---\ntitle: secret frontmatter\ntags: [a]\n---\n"
        "Intro line\n\n# First\nalpha\n\n```\n# not a heading\n```\n## Second\nbeta\n"
    )
    parts = run(md.encode(), "text/markdown", "text")
    assert [p.heading for p in parts] == [None, "First", "Second"]
    assert "frontmatter" not in "".join(p.body for p in parts)
    assert "# not a heading" in parts[1].body
    assert parts[2].body.startswith("## Second")


def test_html_drops_scripts_and_styles_and_splits_at_headings() -> None:
    html = (
        "<html><head><title>T</title><style>.x{color:red}</style></head><body>"
        "<p>Intro &amp; more</p><script>alert('no')</script>"
        "<h2>Costs</h2><p>Total <b>42</b> EUR</p></body></html>"
    )
    parts = run(html.encode(), "text/html", "text")
    text = " ".join(p.body for p in parts)
    assert "alert" not in text
    assert "color" not in text
    assert parts[0].body == "Intro & more"
    assert parts[-1].heading == "Costs"
    assert "Total 42 EUR" in parts[-1].body


# --- PDF ---------------------------------------------------------------------------


def test_pdf_text_per_page() -> None:
    parts = run(make_pdf(["Invoice ACME (2025)", "Second page total"]), "application/pdf")
    assert [p.page for p in parts] == [1, 2]
    assert "Invoice ACME (2025)" in parts[0].body
    assert "Second page total" in parts[1].body


def test_pdf_page_limit() -> None:
    out = extract(
        io.BytesIO(make_pdf(["one", "two", "three"])),
        mime="application/pdf",
        kind="document",
        limits=Limits(max_text_chars=1000, max_pages=2),
    )
    assert out.truncated
    assert [p.page for p in out.parts] == [1, 2]


def test_encrypted_pdf_is_refused() -> None:
    with pytest.raises(EncryptedDocument):
        run(make_encrypted_pdf(["hidden words"]), "application/pdf")


def test_pdf_with_only_an_owner_password_is_read() -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["restricted but readable"]))))
    writer.encrypt("", "owner-password", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    assert "restricted but readable" in run(out.getvalue(), "application/pdf")[0].body


def test_garbage_pdf_raises() -> None:
    with pytest.raises(Exception):  # noqa: B017,PT011 -- whatever pypdf raises
        run(b"%PDF-1.4 not really", "application/pdf")


# --- office documents ------------------------------------------------------------


def test_docx_paragraphs_headings_and_tabs() -> None:
    data = make_docx(
        [(None, "Preface text"), ("Heading1", "Budget"), (None, "Rent\t900"), ("Title", "End")]
    )
    parts = run(data, DOCX)
    assert [p.heading for p in parts] == [None, "Budget", "End"]
    assert parts[1].body == "Budget\nRent\t900"


def test_docx_entity_declarations_are_refused() -> None:
    bomb = '<!DOCTYPE w:document [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
    with pytest.raises(UnsafeDocument):
        run(make_docx([(None, "x")], doctype=bomb), DOCX)


def test_docx_member_over_the_limit_is_refused() -> None:
    data = make_docx([(None, "word " * 5000)])
    with pytest.raises(UnsafeDocument):
        run(data, DOCX, limits=Limits(max_text_chars=10**6, max_pages=1, max_member_bytes=1000))


def test_a_zip_without_the_document_member_is_not_text() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(NotText):
        run(out.getvalue(), DOCX)


def test_odt_headings_and_spaces() -> None:
    parts = run(make_odt([(False, "Before"), (True, "Chapter"), (False, "two  spaces")]), ODT)
    assert [p.heading for p in parts] == [None, "Chapter"]
    assert parts[1].body == "Chapter\ntwo  spaces"


# --- chunker -----------------------------------------------------------------------


def test_short_sections_on_a_page_are_packed_together() -> None:
    parts = [TextPart("# A\nalpha", heading="A"), TextPart("# B\nbeta", heading="B")]
    chunks = chunker.chunk_parts(parts, size=1000, overlap=100)
    assert len(chunks) == 1
    assert chunks[0].heading == "A"
    assert "beta" in chunks[0].body


def test_chunks_never_cross_pages() -> None:
    parts = [TextPart("one", page=1), TextPart("two", page=2)]
    chunks = chunker.chunk_parts(parts, size=1000, overlap=100)
    assert [(c.page, c.body) for c in chunks] == [(1, "one"), (2, "two")]


def test_long_text_is_split_with_overlap_and_size_limit() -> None:
    sentences = " ".join(f"Sentence number {i} says something useful." for i in range(200))
    chunks = chunker.chunk_parts([TextPart(sentences)], size=300, overlap=60)
    assert len(chunks) > 10
    assert all(len(c.body) <= 300 for c in chunks)
    # Each later chunk starts with words from the end of the previous one.
    first, second = chunks[0].body, chunks[1].body
    assert second.split()[0] in first.split()[-12:]
    # Nothing is lost.
    assert all(f"number {i} " in " ".join(c.body for c in chunks) for i in range(200))


def test_a_huge_word_is_cut() -> None:
    chunks = chunker.chunk_parts([TextPart("x" * 1000)], size=300, overlap=0)
    assert [len(c.body) for c in chunks] == [300, 300, 300, 100]


def test_empty_parts_give_no_chunks() -> None:
    assert chunker.chunk_parts([TextPart("  \n "), TextPart("")], size=300, overlap=0) == []


def test_bad_chunk_settings() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunker.chunk_parts([], size=300, overlap=200)
