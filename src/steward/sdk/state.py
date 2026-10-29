# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Access to `state.db` for a module's own tables.

A module that registers `migrations` keeps its rows in `<module>_*` tables of
`state.db`. Its job handlers, web pages, share-link handler and action types reach
them through a `StateAccess` handle (`JobContext.state`, `Web.state`,
`ShareRequest.state`, `ReadOnlyFS.read_state()`); changes that belong to an action go
through `GuardedFS.write_state` so they land in the same transaction as its journal
entry. Only touch your own tables: the core's tables aren't part of the SDK.

Timestamps are stored with `UTCDateTime` (aware UTC datetimes as fixed-width text, so
comparing the text compares the times).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Connection, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC datetime stored as fixed-width ISO 8601 text.

    Fixed width means string comparison in SQL equals time comparison, and the values
    stay readable in any SQLite browser. Naive datetimes are rejected.
    """

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC datetimes")
        return value.astimezone(UTC).strftime(_FORMAT)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, _FORMAT).replace(tzinfo=UTC)


class StateAccess(Protocol):
    """Connections to `state.db`."""

    def read(self) -> AbstractContextManager[Connection]:
        """A read transaction (a consistent snapshot)."""
        ...

    def write(self) -> AbstractContextManager[Connection]:
        """A write transaction (`BEGIN IMMEDIATE`; keep it short)."""
        ...
