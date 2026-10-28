# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Schedules: when the scheduler enqueues a recurring job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta


@dataclass(frozen=True, slots=True)
class Every:
    """Run at a fixed interval, measured from the previous run."""

    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval < timedelta(minutes=1):
            raise ValueError("schedule interval must be at least 1 minute")

    def __str__(self) -> str:
        return f"every {format_interval(self.interval)}"


@dataclass(frozen=True, slots=True)
class Daily:
    """Run once a day at a local time (the timezone is set in `steward.yaml`)."""

    at: time

    def __str__(self) -> str:
        return f"daily {self.at.strftime('%H:%M')}"


When = Every | Daily


def format_interval(interval: timedelta) -> str:
    seconds = int(interval.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
