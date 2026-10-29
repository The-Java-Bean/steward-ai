# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Zip files built while they download: no temporary file, no size limit (ZIP64), no
compression (most large files are compressed already)."""

from __future__ import annotations

import io
import logging
import os
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime

from steward.core.files.browse import READ_CHUNK, ZipMember, open_member
from steward.sdk.web import NotFound

log = logging.getLogger(__name__)

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class _Sink(io.RawIOBase):
    """A write-only, non-seekable stream whose bytes are taken out as they arrive."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._written = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        chunk = bytes(data)
        self._chunks.append(chunk)
        self._written += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._written

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _date_time(when: datetime) -> tuple[int, int, int, int, int, int]:
    stamp = when.timetuple()[:6]
    return max(stamp, ZIP_EPOCH)


def stream_zip(members: Iterable[ZipMember]) -> Iterator[bytes]:
    """Yield the zip's bytes. Files that vanish or can't be read are left out (logged by
    type only, never by name)."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for member in members:
            info = zipfile.ZipInfo(member.arcname, _date_time(member.modified))
            if member.location is None:
                info.external_attr = 0o40755 << 16 | 0x10
                zf.writestr(info, b"")
            else:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                try:
                    fd = open_member(member)
                except (NotFound, OSError) as exc:
                    log.warning(
                        "zip: file skipped", extra={"fields": {"error": type(exc).__name__}}
                    )
                    continue
                try:
                    with zf.open(info, "w", force_zip64=True) as out:
                        while chunk := os.read(fd, READ_CHUNK):
                            out.write(chunk)
                            if data := sink.take():
                                yield data
                finally:
                    os.close(fd)
            if data := sink.take():
                yield data
    if data := sink.take():
        yield data
