# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: eval results and baselines.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("run_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("set_hash", sa.Text, nullable=False),
        sa.Column("cases", sa.Integer, nullable=False),
        sa.Column("metrics", sa.JSON, nullable=False),
        sa.Column("failed", sa.JSON, nullable=False),
        sa.Column("seconds", sa.Float, nullable=False),
        sa.Column("baseline", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    op.create_index("ix_eval_runs_role", "eval_runs", ["role", "run_id"])


def downgrade() -> None:
    op.drop_table("eval_runs")
