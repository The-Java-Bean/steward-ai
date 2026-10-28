# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Creating folders inside a library without following symlinks."""

from __future__ import annotations

import errno
import os
import threading

from steward.core.folders.paths import normalize_relpath

WRITE_LOCK = threading.Lock()
"""Held for every write to a library, so the API process has a single writer."""

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ExecutorError(Exception):
    """A write was refused (symlink in the way, not a directory, no permission...)."""


def create_folder(root: str, relpath: str, *, mode: int = 0o755) -> bool:
    """Create `relpath` (and missing parents) under the library `root`. Every component
    is opened relative to its parent with `O_NOFOLLOW`, so a symlink inside the library
    can't redirect the write elsewhere. Returns True if the last folder was created."""
    parts = normalize_relpath(relpath).split("/")
    if parts == [""]:
        raise ExecutorError("the library root already exists")
    created = False
    with WRITE_LOCK:
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            raise ExecutorError(f"library root {root} can't be opened: {exc.strerror}") from exc
        try:
            for part in parts:
                created = False
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ExecutorError(f"can't create {part!r}: {exc.strerror}") from exc
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ExecutorError(
                            f"{part!r} is a symlink or a file, not a folder"
                        ) from exc
                    raise ExecutorError(f"can't open {part!r}: {exc.strerror}") from exc
                os.close(fd)
                fd = child
        finally:
            os.close(fd)
    return created
