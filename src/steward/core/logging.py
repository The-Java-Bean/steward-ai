# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Structured JSON logs on stdout. Logs hold IDs, paths and hashes, never file content.

Add fields with `log.info("message", extra={"fields": {"job_id": 7}})`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info and record.exc_info[0] is not None:
            # The exception type only: messages from parsers can quote file content.
            entry["exc_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def configure(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("alembic", "sqlalchemy"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
