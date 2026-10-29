# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Text extractors, one per format. They run in the isolated child process
(`StageContext.run_isolated`), get the opened file and return `Extracted`.

Extractors raise `NotText` for content that turns out not to be text and
`EncryptedDocument` for documents that need a password; the stage records those as
skipped. Other exceptions mean the file couldn't be parsed (also skipped, by exception
type name only: messages can quote file content).
"""

from __future__ import annotations

import codecs
import logging
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import BinaryIO
from xml.parsers import expat

from pypdf import PdfReader

from steward.sdk import TextPart

VERSION = 1
"""Raise when any extractor's output changes; it's part of `documents.extract`'s version."""


class NotText(Exception):
    """The content isn't text (binary data in a file named like text)."""


class EncryptedDocument(Exception):
    """The document is encrypted and has no empty password."""


class UnsafeDocument(Exception):
    """The document uses something refused for safety (XML entity declarations, a
    member bigger than the limit)."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_text_chars: int
    max_pages: int
    max_member_bytes: int = 256 * 1024 * 1024
    """Largest uncompressed XML member read from a DOCX/ODT."""


@dataclass(slots=True)
class Extracted:
    parts: list[TextPart] = field(default_factory=list)
    truncated: bool = False
    """Stopped at `max_text_chars` or `max_pages`."""


class _Collector:
    """Collects parts up to the character limit."""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.out = Extracted()
        self.chars = 0

    @property
    def full(self) -> bool:
        return self.out.truncated

    def add(self, body: str, *, page: int | None = None, heading: str | None = None) -> None:
        if self.full:
            return
        room = self.limits.max_text_chars - self.chars
        if len(body) > room:
            body = body[:room]
            self.out.truncated = True
        if body.strip() or heading:
            self.out.parts.append(TextPart(body, page=page, heading=heading))
            self.chars += len(body)


# --- plain text ------------------------------------------------------------------

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode(data: bytes) -> str:
    """UTF-8 (or a BOM's encoding), else cp1252 with replacements. Raises `NotText` for
    data with NUL bytes and no BOM."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    if b"\x00" in data[:8192]:
        raise NotText
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _read_text(f: BinaryIO, limits: Limits) -> tuple[str, bool]:
    # UTF-32 needs up to 4 bytes per character; read enough, cut after decoding.
    data = f.read(limits.max_text_chars * 4 + 4)
    text = decode(data)
    if len(text) > limits.max_text_chars:
        return text[: limits.max_text_chars], True
    return text, bool(f.read(1))


def extract_plain(f: BinaryIO, limits: Limits) -> Extracted:
    text, truncated = _read_text(f, limits)
    out = _Collector(limits)
    out.add(text)
    out.out.truncated = out.out.truncated or truncated
    return out.out


# --- Markdown --------------------------------------------------------------------

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def extract_markdown(f: BinaryIO, limits: Limits) -> Extracted:
    text, truncated = _read_text(f, limits)
    text = _FRONTMATTER.sub("", text, count=1)
    out = _Collector(limits)
    heading: str | None = None
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = None
        match = _ATX.match(line) if fence is None else None
        if match:
            out.add("\n".join(lines), heading=heading)
            lines = []
            heading = match.group(2).strip() or None
        lines.append(line)
    out.add("\n".join(lines), heading=heading)
    out.out.truncated = out.out.truncated or truncated
    return out.out


# --- HTML ------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCKS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
        "header", "footer", "blockquote", "pre", "dd", "dt", "hr", "td", "th", "title",
    }
)  # fmt: skip


class _HtmlText(HTMLParser):
    def __init__(self, out: _Collector) -> None:
        super().__init__(convert_charrefs=True)
        self.result = out
        self.skip = 0
        self.heading: str | None = None
        self.in_heading = False
        self.heading_text: list[str] = []
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _HEADINGS and not self.skip:
            self.flush()
            self.in_heading = True
            self.heading_text = []
        elif tag in _BLOCKS:
            self.buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _HEADINGS and self.in_heading:
            self.in_heading = False
            self.heading = " ".join("".join(self.heading_text).split()) or None
            self.buf.append("\n")
        elif tag in _BLOCKS:
            self.buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.buf.append(data)
        if self.in_heading:
            self.heading_text.append(data)

    def flush(self) -> None:
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.buf))
        text = re.sub(r" ?\n[ \n]*", "\n", text).strip()
        self.result.add(text, heading=self.heading)
        self.buf = []


def extract_html(f: BinaryIO, limits: Limits) -> Extracted:
    text, truncated = _read_text(f, limits)
    out = _Collector(limits)
    parser = _HtmlText(out)
    parser.feed(text)
    parser.close()
    parser.flush()
    out.out.truncated = out.out.truncated or truncated
    return out.out


# --- PDF -------------------------------------------------------------------------


def extract_pdf(f: BinaryIO, limits: Limits) -> Extracted:

    reader = PdfReader(f, strict=False)
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise EncryptedDocument
        except EncryptedDocument:
            raise
        except Exception:
            raise EncryptedDocument from None
    out = _Collector(limits)
    for number, page in enumerate(reader.pages, start=1):
        if number > limits.max_pages:
            out.out.truncated = True
            break
        out.add(page.extract_text() or "", page=number)
        if out.full:
            break
    return out.out


# --- office documents (zip + XML) ------------------------------------------------


def _xml_parser() -> expat.XMLParserType:
    """An expat parser that refuses DTD entity declarations (no entity-expansion bombs,
    no external entities) and reports names as `namespace local`."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*args: object) -> None:
        raise UnsafeDocument

    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse  # type: ignore[assignment]
    return parser


def _parse_member(f: BinaryIO, member: str, limits: Limits, parser: expat.XMLParserType) -> None:
    with zipfile.ZipFile(f) as archive:
        try:
            info = archive.getinfo(member)
        except KeyError:
            raise NotText from None
        if info.file_size > limits.max_member_bytes:
            raise UnsafeDocument
        read = 0
        with archive.open(info) as data:
            while chunk := data.read(1024 * 1024):
                read += len(chunk)
                if read > limits.max_member_bytes:  # the header lied
                    raise UnsafeDocument
                parser.Parse(chunk, False)
        parser.Parse(b"", True)


def _local(name: str) -> str:
    return name.rsplit(" ", 1)[-1]


class _Paragraphs:
    """Turns paragraph events into sections that start at each heading."""

    def __init__(self, out: _Collector) -> None:
        self.out = out
        self.heading: str | None = None
        self.lines: list[str] = []
        self.text: list[str] = []

    def end_paragraph(self, *, is_heading: bool) -> None:
        line = "".join(self.text).strip()
        self.text = []
        if is_heading and line:
            self.flush()
            self.heading = line
        self.lines.append(line)

    def flush(self) -> None:
        self.out.add("\n".join(self.lines).strip(), heading=self.heading)
        self.lines = []


_DOCX_HEADING = re.compile(r"^(heading\s*\d|title)$", re.I)


def extract_docx(f: BinaryIO, limits: Limits) -> Extracted:
    out = _Collector(limits)
    paras = _Paragraphs(out)
    parser = _xml_parser()
    state = {"in_text": False, "heading": False}

    def start(name: str, attrs: dict[str, str]) -> None:
        tag = _local(name)
        if tag == "t":
            state["in_text"] = True
        elif tag == "tab":
            paras.text.append("\t")
        elif tag in ("br", "cr"):
            paras.text.append("\n")
        elif tag == "pStyle":
            value = next((v for k, v in attrs.items() if _local(k) == "val"), "")
            state["heading"] = bool(_DOCX_HEADING.match(value))
        elif tag == "outlineLvl":
            state["heading"] = True

    def end(name: str) -> None:
        tag = _local(name)
        if tag == "t":
            state["in_text"] = False
        elif tag == "p":
            paras.end_paragraph(is_heading=state["heading"])
            state["heading"] = False

    def data(text: str) -> None:
        if state["in_text"]:
            paras.text.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    _parse_member(f, "word/document.xml", limits, parser)
    paras.flush()
    return out.out


def extract_odt(f: BinaryIO, limits: Limits) -> Extracted:
    out = _Collector(limits)
    paras = _Paragraphs(out)
    parser = _xml_parser()
    depth = {"p": 0, "h": 0}

    def start(name: str, attrs: dict[str, str]) -> None:
        tag = _local(name)
        if tag in depth:
            depth[tag] += 1
        elif tag == "s":
            count = next((v for k, v in attrs.items() if _local(k) == "c"), "1")
            paras.text.append(" " * min(int(count) if count.isdigit() else 1, 100))
        elif tag == "tab":
            paras.text.append("\t")
        elif tag == "line-break":
            paras.text.append("\n")

    def end(name: str) -> None:
        tag = _local(name)
        if depth.get(tag):
            depth[tag] -= 1
            if not depth["p"] and not depth["h"]:
                paras.end_paragraph(is_heading=tag == "h")

    def data(text: str) -> None:
        if depth["p"] or depth["h"]:
            paras.text.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    _parse_member(f, "content.xml", limits, parser)
    paras.flush()
    return out.out


# --- choosing one -----------------------------------------------------------------

Extractor = Callable[[BinaryIO, Limits], Extracted]

BY_MIME: dict[str, Extractor] = {
    "text/markdown": extract_markdown,
    "text/html": extract_html,
    "application/xhtml+xml": extract_html,
    "application/pdf": extract_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx,
    "application/vnd.oasis.opendocument.text": extract_odt,
}


def extractor_for(mime: str, kind: str) -> Extractor | None:
    if mime in BY_MIME:
        return BY_MIME[mime]
    if kind == "text":
        return extract_plain
    return None


def extract(f: BinaryIO, *, mime: str, kind: str, limits: Limits) -> Extracted:
    """Entry point in the isolated child. Logging is off there: parser warnings could
    quote file content and nobody reads the child's stderr."""
    logging.disable(logging.CRITICAL)
    fn = extractor_for(mime, kind)
    if fn is None:
        raise NotText
    return fn(f, limits)
