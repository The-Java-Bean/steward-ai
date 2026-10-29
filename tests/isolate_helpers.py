# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Functions `run_isolated` runs in a child process (they must be importable by name)."""

from __future__ import annotations

import os
import time
from typing import BinaryIO


def read_all(f: BinaryIO) -> bytes:
    return f.read()


def sleep_forever(f: BinaryIO) -> None:
    time.sleep(3600)


def spin(f: BinaryIO) -> None:
    while True:
        pass


def eat_memory(f: BinaryIO) -> int:
    hog = [bytearray(64 * 1024 * 1024) for _ in range(64)]  # 4 GiB
    return len(hog)


def die(f: BinaryIO) -> None:
    os._exit(3)


def leak_content(f: BinaryIO) -> None:
    raise ValueError(f"content: {f.read().decode()}")


def pid(f: BinaryIO) -> int:
    return os.getpid()
