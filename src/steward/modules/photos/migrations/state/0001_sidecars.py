# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""photos: what steward-ai last wanted in, and itself added to, each XMP sidecar.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.sdk.state import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "photos_sidecars",
        sa.Column("file_id", sa.Text, primary_key=True),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )
    op.create_table(
        "photos_sidecar_runs",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("revision", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("photos_sidecar_runs")
    op.drop_table("photos_sidecars")
