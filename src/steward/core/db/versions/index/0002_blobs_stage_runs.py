# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: blobs (one row per unique content) and stage runs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("content_hash", sa.Text, primary_key=True),
        sa.Column("hash_kind", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("hash_kind IN ('full', 'sampled')", name="ck_blobs_hash_kind"),
    )
    op.create_table(
        "stage_runs",
        sa.Column(
            "content_hash",
            sa.Text,
            sa.ForeignKey("blobs.content_hash", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("stage", sa.Text, primary_key=True),
        sa.Column("stage_version", sa.Integer, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("status IN ('done', 'failed', 'skipped')", name="ck_stage_runs_status"),
    )
    op.create_index("ix_stage_runs_stage", "stage_runs", ["stage", "status"])


def downgrade() -> None:
    op.drop_table("stage_runs")
    op.drop_table("blobs")
