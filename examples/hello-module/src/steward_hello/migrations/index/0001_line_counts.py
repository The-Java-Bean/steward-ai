# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""hello: line counts of text files (output of the `hello.lines` stage).

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hello_line_counts",
        sa.Column("content_hash", sa.Text, primary_key=True),
        sa.Column("lines", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hello_line_counts")
