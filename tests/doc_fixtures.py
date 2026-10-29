# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Small, valid documents built in code (no binary fixtures in the repository)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _pdf_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: Sequence[str]) -> bytes:
    """A PDF with one page per string (lines become separate text lines, Helvetica)."""
    objects: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        lines = text.split("\n")
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in lines:
            ops.append(f"({_pdf_string(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode()
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_encrypted_pdf(pages: Sequence[str], password: str = "secret-pw") -> bytes:  # noqa: S107
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(pages))))
    writer.encrypt(password, algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _zip(members: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_docx(paragraphs: Sequence[tuple[str | None, str]], *, doctype: str = "") -> bytes:
    """`paragraphs` = (style or None, text); style `Heading1` makes a heading."""
    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        runs = "<w:tab/>".join(f"<w:r><w:t>{_xml_escape(t)}</w:t></w:r>" for t in text.split("\t"))
        body.append(f"<w:p>{ppr}{runs}</w:p>")
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{doctype}'
        f'<w:document xmlns:w="{W_NS}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    return _zip(
        {
            "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
            "word/document.xml": document,
        }
    )


def make_odt(paragraphs: Sequence[tuple[bool, str]]) -> bytes:
    """`paragraphs` = (is_heading, text)."""
    ns = (
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    )
    body = "".join(
        f'<text:h text:outline-level="1">{_xml_escape(t)}</text:h>'
        if heading
        else f"<text:p>{_xml_escape(t).replace('  ', ' <text:s/>')}</text:p>"
        for heading, t in paragraphs
    )
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ns}>'
        f"<office:body><office:text>{body}</office:text></office:body></office:document-content>"
    )
    return _zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})
