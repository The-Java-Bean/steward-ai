# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from steward.core.discovery import detect_mime, hash_file, kind_of
from steward.core.discovery.filters import is_ignored
from steward.core.discovery.hashing import SAMPLE, Throttle


def test_full_hash_is_stable_and_content_based(tmp_path: Path) -> None:
    a, b = tmp_path / "a.txt", tmp_path / "copy-with-another-name.txt"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    ha, hb = hash_file(a, "full"), hash_file(b, "full")
    assert ha.content_hash == hb.content_hash
    assert ha.content_hash.startswith("b3:")
    assert (ha.hash_kind, ha.size, ha.mime, ha.kind) == ("full", 12, "text/plain", "text")


def test_sampled_hash_only_for_big_files(tmp_path: Path) -> None:
    small = tmp_path / "small.bin"
    small.write_bytes(os.urandom(SAMPLE))
    assert hash_file(small, "sampled").hash_kind == "full"
    assert hash_file(small, "sampled").content_hash == hash_file(small, "full").content_hash

    big = tmp_path / "big.bin"
    data = bytearray(os.urandom(4 * SAMPLE))
    big.write_bytes(data)
    sampled = hash_file(big, "sampled")
    assert sampled.hash_kind == "sampled"
    assert sampled.content_hash.startswith("b3s:")
    assert sampled.content_hash != hash_file(big, "full").content_hash
    # A byte outside the sampled regions doesn't change a sampled hash...
    data[SAMPLE + 10] ^= 0xFF
    big.write_bytes(data)
    assert hash_file(big, "sampled").content_hash == sampled.content_hash
    # ...but one inside them does.
    data[5] ^= 0xFF
    big.write_bytes(data)
    assert hash_file(big, "sampled").content_hash != sampled.content_hash


@pytest.mark.parametrize(
    ("name", "head", "mime", "kind"),
    [
        ("r.pdf", b"", "application/pdf", "document"),
        ("noext", b"%PDF-1.7", "application/pdf", "document"),
        ("IMG_1.HEIC", b"", "image/heic", "image"),
        ("photo", b"\xff\xd8\xff\xe0", "image/jpeg", "image"),
        ("clip", b"\x00\x00\x00\x18ftypisom", "video/mp4", "video"),
        ("pic", b"\x00\x00\x00\x18ftypheic", "image/heic", "image"),
        ("x.docx", b"PK\x03\x04", None, "document"),
        ("backup.zip", b"", "application/zip", "archive"),
        ("notes.md", b"", "text/markdown", "text"),
        ("data.json", b"", "application/json", "text"),
        ("blob", b"\x00\x01", "application/octet-stream", "other"),
        ("app.ts", b"const x: number = 1;\n", "text/plain", "text"),
        ("App.tsx", b"export const A = () => <div/>;\n", "text/plain", "text"),
        ("a.scss", b".a { color: red; }\n", "text/plain", "text"),
        ("Dockerfile", b"FROM scratch\n", "text/plain", "text"),
        ("cut.ts", "é".encode() * 2047 + b"\xc3", "text/plain", "text"),
        ("clip.ts", b"\x47\x40\x00\x10\x00\x00\xb0\x0d", "application/octet-stream", "other"),
        ("old.foo", b"caf\xe9 au lait", "application/octet-stream", "other"),
        ("empty.foo", b"", "application/octet-stream", "other"),
        ("app.log", b"started\n", "application/octet-stream", "other"),
        ("app.LOG.1", b"started\n", "application/octet-stream", "other"),
        ("IMG_1.CR3", b"", "image/x-canon-cr3", "image"),
        ("IMG_1", b"\x00\x00\x00\x18ftypcrx ", "image/x-canon-cr3", "image"),
        ("shot.rw2", b"", "image/x-panasonic-rw2", "image"),
    ],
)
def test_mime_detection(name: str, head: bytes, mime: str | None, kind: str) -> None:
    detected = detect_mime(name, head)
    if mime is not None:
        assert detected == mime
    assert kind_of(detected) == kind


def test_throttle_limits_rate() -> None:
    throttle = Throttle(10)  # MiB/s
    started = time.monotonic()
    throttle.consume(2 * 1024 * 1024)
    assert time.monotonic() - started >= 0.15
    unlimited = Throttle(0)
    started = time.monotonic()
    unlimited.consume(10**9)
    assert time.monotonic() - started < 0.05


@pytest.mark.parametrize(
    ("relpath", "ignored"),
    [
        ("Downloads/file.part", True),
        ("~$report.docx", True),
        ("a/.~lock.x#", True),
        ("Vault/.obsidian/workspace.json", True),
        ("Vault/.obsidian/workspace-mobile.json", True),
        ("Vault/other/workspace.json", False),
        ("Vault/.obsidian/app.json", False),
        ("report.pdf", False),
    ],
)
def test_ignore_patterns(relpath: str, ignored: bool) -> None:
    from steward.core.config import DiscoverySettings

    assert is_ignored(relpath, DiscoverySettings().ignore_patterns) is ignored


def test_mime_types_dont_depend_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container without /etc/mime.types must detect the same types as a desktop."""
    import mimetypes

    def no_system_files() -> None:
        raise AssertionError("the host's mime.types must not be read")

    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: no_system_files())
    assert detect_mime("a.docx", b"PK\x03\x04") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert detect_mime("a.odt", b"PK\x03\x04") == "application/vnd.oasis.opendocument.text"
    assert kind_of(detect_mime("a.xlsx", b"PK\x03\x04")) == "document"
    assert detect_mime("a.pdf", b"") == "application/pdf"
