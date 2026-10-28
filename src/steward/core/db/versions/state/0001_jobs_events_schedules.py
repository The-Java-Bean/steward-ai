# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: job queue, event outbox, subscriber cursors, schedules.

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("lane", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("run_after", UTCDateTime, nullable=False),
        sa.Column("lease_until", UTCDateTime),
        sa.Column("worker_id", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("dedupe_key", sa.Text),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.Column("finished_at", UTCDateTime),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'dead')", name="ck_jobs_status"
        ),
    )
    op.create_index("ix_jobs_claim", "jobs", ["lane", "status", "run_after", "priority"])
    op.create_index("ix_jobs_finished", "jobs", ["status", "finished_at"])
    op.create_index(
        "ux_jobs_active_dedupe",
        "jobs",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "events",
        sa.Column("event_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("occurred_at", UTCDateTime, nullable=False),
    )
    op.create_index("ix_events_type", "events", ["type", "event_id"])
    op.create_index("ix_events_occurred", "events", ["occurred_at"])

    op.create_table(
        "event_cursors",
        sa.Column("subscriber", sa.Text, primary_key=True),
        sa.Column("last_event_id", sa.Integer, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )

    op.create_table(
        "schedules",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("spec", sa.Text, nullable=False),
        sa.Column("last_run_at", UTCDateTime),
        sa.Column("next_run_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("event_cursors")
    op.drop_table("events")
    op.drop_table("jobs")
