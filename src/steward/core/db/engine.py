# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""SQLite connections: WAL mode, foreign keys, and explicit read/write transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.pool import ConnectionPoolEntry

BUSY_TIMEOUT_MS = 10_000


class Database:
    """One SQLite database file.

    Use `write()` for anything that changes data: it takes SQLite's write lock up front
    (`BEGIN IMMEDIATE`), so concurrent writers wait instead of failing halfway. Use
    `read()` for queries; readers never block writers in WAL mode.
    """

    def __init__(self, path: Path, *, name: str) -> None:
        self.path = path
        self.name = name
        self.engine: Engine = create_engine(f"sqlite:///{path}")
        event.listen(self.engine, "connect", _on_connect)
        event.listen(self.engine, "begin", _on_begin)

    @contextmanager
    def write(self) -> Iterator[Connection]:
        conn = self.engine.connect().execution_options(steward_begin="IMMEDIATE")
        with conn, conn.begin():
            yield conn

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as conn, conn.begin():
            yield conn

    def exists(self) -> bool:
        return self.path.exists()

    def dispose(self) -> None:
        self.engine.dispose()

    def __repr__(self) -> str:
        return f"Database({self.name!r}, {self.path})"


def _on_connect(dbapi_conn: sqlite3.Connection, record: ConnectionPoolEntry) -> None:
    # Let SQLAlchemy's "begin" event issue BEGIN, instead of the driver's implicit one.
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cur.close()


def _on_begin(conn: Connection) -> None:
    mode: Any = conn.get_execution_options().get("steward_begin", "DEFERRED")
    conn.exec_driver_sql(f"BEGIN {'IMMEDIATE' if mode == 'IMMEDIATE' else 'DEFERRED'}")
