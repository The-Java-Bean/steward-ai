# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Health checks: how a module reports whether it (or the program it talks to) works."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

HealthStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    detail: str = ""


HealthCheck = Callable[[], Coroutine[Any, Any, HealthResult]]
"""An `async def` with no arguments. It must not raise; report problems as `fail`."""
