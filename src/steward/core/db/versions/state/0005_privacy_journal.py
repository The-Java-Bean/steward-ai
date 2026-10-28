# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: per-file sensitivity overrides and the action journal.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_sensitivity",
        sa.Column(
            "file_id",
            sa.Text,
            sa.ForeignKey("files.file_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sensitivity", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "sensitivity IN ('local-only', 'excluded')", name="ck_file_sensitivity_level"
        ),
    )
    op.create_table(
        "journal",
        sa.Column("entry_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("plan", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
        sa.Column("inverse", sa.JSON),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("sensitive", sa.Boolean, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("proposal_id", sa.Integer),
        sa.Column("undo_of", sa.Integer, sa.ForeignKey("journal.entry_id", ondelete="RESTRICT")),
        sa.Column("undone_by", sa.Integer, sa.ForeignKey("journal.entry_id", ondelete="RESTRICT")),
        sa.Column("applied_at", UTCDateTime, nullable=False),
    )
    op.create_index("ix_journal_applied", "journal", ["applied_at"])


def downgrade() -> None:
    op.drop_table("journal")
    op.drop_table("file_sensitivity")
