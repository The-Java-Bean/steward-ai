# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Idle disk priority for the calling thread (Linux `ioprio_set`), so hashing yields
to everything else using the disk. Best effort: elsewhere it does nothing."""

from __future__ import annotations

import ctypes
import logging
import platform
import sys

log = logging.getLogger(__name__)

_SYSCALL = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "i686": 289}
_IOPRIO_WHO_PROCESS = 1  # with id 0: the calling thread
_IOPRIO_CLASS_IDLE = 3
_IOPRIO_CLASS_SHIFT = 13


def set_idle_io_priority() -> bool:
    """Returns True if the calling thread now has idle IO priority."""
    number = _SYSCALL.get(platform.machine())
    if not sys.platform.startswith("linux") or number is None:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = _IOPRIO_CLASS_IDLE << _IOPRIO_CLASS_SHIFT
        result = libc.syscall(number, _IOPRIO_WHO_PROCESS, 0, value)
    except (OSError, AttributeError):
        return False
    if result != 0:
        log.info("couldn't set idle IO priority", extra={"fields": {"errno": ctypes.get_errno()}})
        return False
    return True
