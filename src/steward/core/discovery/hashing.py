# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `hash` stage: content hashes, MIME types and coarse kinds.

Content hashes are BLAKE3. A **full** hash reads the whole file and is written
`b3:<hex>`. A **sampled** hash (for folders whose type says `hash: sampled`, e.g. games
and archives on spinning disks) reads the size plus the first, middle and last MiB and
is written `b3s:<hex>`; it can't prove two files are identical, so identity checks
using it also compare size and mtime. Files small enough to sample completely get a
full hash either way, so their hashes match copies elsewhere.
"""

from __future__ import annotations

import codecs
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from blake3 import blake3

HashKind = Literal["full", "sampled"]
HASH_STAGE = "core.hash"
HASH_STAGE_VERSION = 1
CHUNK = 1024 * 1024
SAMPLE = 1024 * 1024
HEAD = 4096
PREFIX: dict[HashKind, str] = {"full": "b3:", "sampled": "b3s:"}


@dataclass(frozen=True, slots=True)
class HashResult:
    content_hash: str
    hash_kind: HashKind
    size: int
    mime: str
    kind: str


class Throttle:
    """Keeps reads under a rate (MiB/s), shared by every file a scan hashes."""

    def __init__(self, mib_per_second: float) -> None:
        self.rate = mib_per_second * 1024 * 1024
        self.started = time.monotonic()
        self.consumed = 0

    def consume(self, nbytes: int) -> None:
        if self.rate <= 0:
            return
        self.consumed += nbytes
        ahead = self.consumed / self.rate - (time.monotonic() - self.started)
        if ahead > 0:
            time.sleep(ahead)


NO_THROTTLE = Throttle(0)


def hash_file(path: Path, policy: HashKind, *, throttle: Throttle = NO_THROTTLE) -> HashResult:
    """Hash `path` (which must not be a symlink). Raises OSError if it can't be read."""
    with path.open("rb") as f:
        size = _size(f)
        if policy == "sampled" and size > 3 * SAMPLE:
            head, digest = _sampled(f, size, throttle)
            kind: HashKind = "sampled"
        else:
            head, digest = _full(f, throttle)
            kind = "full"
    mime = detect_mime(path.name, head)
    return HashResult(PREFIX[kind] + digest, kind, size, mime, kind_of(mime))


def _size(f: BinaryIO) -> int:
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    return size


def _full(f: BinaryIO, throttle: Throttle) -> tuple[bytes, str]:
    hasher = blake3()
    head = b""
    while chunk := f.read(CHUNK):
        if not head:
            head = chunk[:HEAD]
        hasher.update(chunk)
        throttle.consume(len(chunk))
    return head, hasher.hexdigest()


def _sampled(f: BinaryIO, size: int, throttle: Throttle) -> tuple[bytes, str]:
    hasher = blake3()
    hasher.update(size.to_bytes(8, "little"))
    head = b""
    for offset in (0, size // 2 - SAMPLE // 2, size - SAMPLE):
        f.seek(offset)
        chunk = f.read(SAMPLE)
        if not head:
            head = chunk[:HEAD]
        hasher.update(chunk)
        throttle.consume(len(chunk))
    return head, hasher.hexdigest()


# --- MIME types and kinds -----------------------------------------------------------

# Python's built-in table plus ours, never the host's /etc/mime.types: the same file must
# get the same MIME type on every machine (a slim container image has no such file).
_MIMETYPES = mimetypes.MimeTypes(filenames=())

_EXTRA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".rtf": "application/rtf",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".xmp": "application/rdf+xml",  # sidecars: metadata, not a document to index
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".epub": "application/epub+zip",
    ".7z": "application/x-7z-compressed",
    ".mkv": "video/x-matroska",
    ".flac": "audio/flac",
    ".dng": "image/x-adobe-dng",
    ".cr2": "image/x-canon-cr2",
    ".cr3": "image/x-canon-cr3",
    ".nef": "image/x-nikon-nef",
    ".arw": "image/x-sony-arw",
    ".raf": "image/x-fuji-raf",
    ".orf": "image/x-olympus-orf",
    ".rw2": "image/x-panasonic-rw2",
    ".pef": "image/x-pentax-pef",
    ".srw": "image/x-samsung-srw",
}

# Magic numbers for files whose name doesn't say what they are.
_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"%PDF-", "application/pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"\x1f\x8b", "application/gzip"),
    (0, b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"fLaC", "audio/flac"),
    (0, b"OggS", "audio/ogg"),
    (0, b"\x1aE\xdf\xa3", "video/x-matroska"),
)

DOCUMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/rtf",
        "application/epub+zip",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)
ARCHIVE_TYPES = frozenset(
    {
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/vnd.rar",
        "application/x-bzip2",
        "application/x-xz",
    }
)
TEXT_LIKE = frozenset({"application/json", "application/yaml", "application/xml"})
_CONTROL_BYTES = bytes(b for b in range(32) if b not in b"\t\n\f\r\x1b")


def detect_mime(name: str, head: bytes) -> str:
    """By file extension, else by magic number, else `text/plain` for what reads as text,
    else `application/octet-stream`."""
    suffix = Path(name).suffix.lower()
    if suffix in _EXTRA_TYPES:
        return _EXTRA_TYPES[suffix]
    guessed, _ = _MIMETYPES.guess_type(name, strict=False)
    if guessed:
        return guessed
    for offset, magic, mime in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return mime
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image/heic"
        if brand == b"crx ":
            return "image/x-canon-cr3"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if ".log" not in (s.lower() for s in Path(name).suffixes) and _reads_as_text(head):
        return "text/plain"
    return "application/octet-stream"


def _reads_as_text(head: bytes) -> bool:
    """Whether the start of a file is UTF-8 text (it may end in the middle of a character).
    Code in any language passes; logs are kept out by the caller, being noisy and often
    holding tokens."""
    if not head or head.translate(None, delete=_CONTROL_BYTES) != head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
    except UnicodeDecodeError:
        return False
    return True


def kind_of(mime: str) -> str:
    """Coarse kind: document, image, video, audio, text, archive or other."""
    major = mime.split("/", 1)[0]
    if major in ("image", "video", "audio", "text"):
        return major
    if mime in DOCUMENT_TYPES or mime.startswith("application/vnd.openxmlformats-officedocument"):
        return "document"
    if mime in ARCHIVE_TYPES:
        return "archive"
    if mime in TEXT_LIKE:
        return "text"
    return "other"
