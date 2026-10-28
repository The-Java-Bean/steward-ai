# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Events: facts about changes, recorded in the outbox and delivered to subscribers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    """One recorded event. Payloads hold IDs and paths, never file content."""

    event_id: int
    type: str
    """`<area>.<name>`, e.g. `file.discovered`; module events are `<module>.<name>`."""
    version: int
    payload: Mapping[str, Any]
    actor: str
    """Who caused it: `user:<id>`, `ai:<role>`, `rule:<id>`, `system`, `module:<name>`."""
    occurred_at: datetime


EventHandler = Callable[[Event], None]
"""Runs as a job on the `events` lane. Delivery is at-least-once, so handlers must be
idempotent. Raise to retry later."""
