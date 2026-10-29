# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Run a function on a file in a separate, resource-limited process.

Parsers are the part of the pipeline most exposed to hostile input (§21: a malicious
file exploiting a parser bug). `run_isolated` gives the parser its own process with an
address-space limit (`RLIMIT_AS`) and a CPU-time limit, opens the file there itself
(`O_NOFOLLOW`, then the same size/mtime check as `StageContext.open`), and kills it at
the wall-clock timeout. The worker only ever receives the pickled result, or a reason
without any message text (messages could quote file content).

Children come from a forkserver (a clean process started once), never from forking the
multi-threaded worker itself. The forkserver preloads the SDK and the module of the
first function it runs, so a child only pays for the fork, not for imports.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import stat
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, BinaryIO

from steward.sdk import FileChanged, IsolationError

OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
POLL_S = 30.0
"""How often a long wait calls `renew` (lease renewal)."""

_METHOD = "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
_CTX: Any = multiprocessing.get_context(_METHOD)
_preloaded = False


def _preload(fn: Callable[..., Any]) -> None:
    """Before the forkserver starts: preload what children would import anyway."""
    global _preloaded
    if _preloaded or _METHOD != "forkserver":
        return
    _preloaded = True
    target = getattr(fn, "func", fn)  # functools.partial
    modules = ["__main__", "steward.sdk", __name__]
    name = getattr(target, "__module__", None)
    if isinstance(name, str) and name not in modules:
        modules.append(name)
    _CTX.set_forkserver_preload(modules)


def run_isolated[T](
    path: Path,
    size: int,
    mtime_ns: int,
    fn: Callable[[BinaryIO], T],
    *,
    timeout_s: float,
    memory_mib: int,
    renew: Callable[[], None] | None = None,
) -> T:
    if timeout_s <= 0 or memory_mib <= 0:
        raise ValueError("timeout_s and memory_mib must be positive")
    _preload(fn)
    receiver, sender = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(
        target=_child,
        args=(sender, str(path), size, mtime_ns, fn, memory_mib, timeout_s),
        daemon=True,
    )
    proc.start()
    sender.close()
    deadline = time.monotonic() + timeout_s
    try:
        message = _wait(receiver, proc, deadline, renew)
    finally:
        receiver.close()
        if proc.is_alive():
            proc.kill()
        proc.join(5)
    kind, value = message
    if kind == "ok":
        return value  # type: ignore[no-any-return]
    if kind == "changed":
        raise FileChanged("file changed since it was scanned")
    if kind == "memory":
        raise IsolationError("too_big")
    raise IsolationError("error", str(value))


def _wait(
    receiver: Connection,
    proc: Any,
    deadline: float,
    renew: Callable[[], None] | None,
) -> tuple[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            raise IsolationError("timed_out")
        if receiver.poll(min(remaining, POLL_S)):
            try:
                return receiver.recv()  # type: ignore[no-any-return]
            except (EOFError, OSError):
                proc.join(5)
                raise _died(proc.exitcode) from None
        if renew is not None:
            renew()


def _died(exitcode: int | None) -> IsolationError:
    if exitcode == -signal.SIGXCPU:
        return IsolationError("timed_out")
    if exitcode == -signal.SIGKILL:
        # The kernel's OOM killer, or an allocation outside Python hitting the limit.
        return IsolationError("too_big")
    return IsolationError("crashed")


def _limit(memory_mib: int, cpu_s: float) -> None:
    try:
        import resource

        mem = memory_mib * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        cpu = int(cpu_s) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    except (ImportError, ValueError, OSError):  # pragma: no cover -- platform dependent
        pass


def _child(
    sender: Connection,
    path: str,
    size: int,
    mtime_ns: int,
    fn: Callable[[BinaryIO], Any],
    memory_mib: int,
    cpu_s: float,
) -> None:
    try:
        _limit(memory_mib, cpu_s)
        try:
            fd = os.open(path, OPEN_FLAGS)
        except OSError:
            sender.send(("changed", None))
            return
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size or st.st_mtime_ns != mtime_ns:
            os.close(fd)
            sender.send(("changed", None))
            return
        with os.fdopen(fd, "rb") as f:
            result = fn(f)
        sender.send(("ok", result))
    except FileChanged:
        sender.send(("changed", None))
    except MemoryError:
        sender.send(("memory", None))
    except BaseException as exc:
        with contextlib.suppress(BaseException):  # the parent then reports a crash
            sender.send(("error", type(exc).__name__))
    finally:
        sender.close()
