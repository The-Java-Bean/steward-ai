# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Background jobs: work a module hands to the job queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from steward.sdk.library import ActionRunner, LibraryAccess
    from steward.sdk.state import StateAccess

Lane = Literal["io", "cpu", "llm-local", "llm-remote", "events", "executor"]
"""Which pool of workers runs a job. `executor` runs in the API process (the only
writer); every other lane runs in the worker process."""

LANES: tuple[Lane, ...] = get_args(Lane)
WORKER_LANES: tuple[Lane, ...] = tuple(lane for lane in LANES if lane != "executor")


class PermanentJobError(Exception):
    """Raise from a job handler when retrying can't help; the job goes straight to `dead`."""


class RetryLater(Exception):
    """Raise from a job handler when the job can't run *yet* (a model server is down…):
    it's queued again after `delay_s` seconds without using up one of its attempts."""

    def __init__(self, reason: str, *, delay_s: float = 60.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.delay_s = max(1.0, float(delay_s))


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a job handler receives."""

    job_id: int
    type: str
    payload: Mapping[str, Any]
    attempt: int
    """1 on the first run, 2 on the first retry, and so on."""
    renew_lease: Callable[[], None] = field(repr=False, compare=False)
    """Call during long work to keep the job from being handed to another worker."""
    state: StateAccess = field(repr=False, compare=False)
    """`state.db`, for the module's own tables (see `steward.sdk.state`)."""
    library: LibraryAccess | None = field(default=None, repr=False, compare=False)
    """The files, their tags and texts (see `steward.sdk.library`)."""
    actions: ActionRunner | None = field(default=None, repr=False, compare=False)
    """Runs actions as the module. Only on the `executor` lane: elsewhere it's None,
    because the worker has no write access to the libraries."""


JobHandler = Callable[[JobContext], None]
"""Runs in a worker thread. Must be idempotent: a job can run more than once (for
example after a crash). Raise to retry later with backoff."""
