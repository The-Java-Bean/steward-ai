# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`run_isolated`: a parser in its own limited process, never trusted with the worker."""

from __future__ import annotations

import os
import time
from pathlib import Path

import isolate_helpers as h
import pytest

from steward.core.stages.isolate import run_isolated
from steward.sdk import FileChanged, IsolationError


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, int, int]:
    path = tmp_path / "file.txt"
    path.write_bytes(b"secret file words")
    st = path.stat()
    return path, st.st_size, st.st_mtime_ns


def test_returns_the_result_from_another_process(sample: tuple[Path, int, int]) -> None:
    assert run_isolated(*sample, h.read_all, timeout_s=30, memory_mib=512) == b"secret file words"
    assert run_isolated(*sample, h.pid, timeout_s=30, memory_mib=512) != os.getpid()


def test_wall_clock_timeout_kills_it(sample: tuple[Path, int, int]) -> None:
    started = time.monotonic()
    with pytest.raises(IsolationError) as info:
        run_isolated(*sample, h.sleep_forever, timeout_s=1, memory_mib=512)
    assert info.value.reason == "timed_out"
    assert time.monotonic() - started < 10


def test_cpu_limit(sample: tuple[Path, int, int]) -> None:
    with pytest.raises(IsolationError) as info:
        run_isolated(*sample, h.spin, timeout_s=2, memory_mib=512)
    assert info.value.reason == "timed_out"


def test_memory_limit(sample: tuple[Path, int, int]) -> None:
    with pytest.raises(IsolationError) as info:
        run_isolated(*sample, h.eat_memory, timeout_s=30, memory_mib=512)
    assert info.value.reason == "too_big"


def test_a_crash_is_reported(sample: tuple[Path, int, int]) -> None:
    with pytest.raises(IsolationError) as info:
        run_isolated(*sample, h.die, timeout_s=30, memory_mib=512)
    assert info.value.reason == "crashed"


def test_errors_carry_only_the_exception_type(sample: tuple[Path, int, int]) -> None:
    with pytest.raises(IsolationError) as info:
        run_isolated(*sample, h.leak_content, timeout_s=30, memory_mib=512)
    assert info.value.reason == "error"
    assert info.value.error_type == "ValueError"
    assert "secret" not in str(info.value)


def test_a_changed_file_is_reported(sample: tuple[Path, int, int]) -> None:
    path, size, mtime_ns = sample
    path.write_bytes(b"something else entirely")
    with pytest.raises(FileChanged):
        run_isolated(path, size, mtime_ns, h.read_all, timeout_s=30, memory_mib=512)


def test_a_symlink_is_never_followed(sample: tuple[Path, int, int], tmp_path: Path) -> None:
    path, size, mtime_ns = sample
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    with pytest.raises(FileChanged):
        run_isolated(link, size, mtime_ns, h.read_all, timeout_s=30, memory_mib=512)


def test_renew_is_called_while_waiting(
    sample: tuple[Path, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from steward.core.stages import isolate

    monkeypatch.setattr(isolate, "POLL_S", 0.2)
    calls: list[int] = []
    with pytest.raises(IsolationError):
        run_isolated(
            *sample, h.sleep_forever, timeout_s=1, memory_mib=512, renew=lambda: calls.append(1)
        )
    assert calls
