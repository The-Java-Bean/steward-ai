# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: embedding spaces and embeddings (docs/adr/0023).

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
        "embedding_spaces",
        sa.Column("space_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("dims", sa.Integer, nullable=False),
        sa.Column("model_key", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("completed_at", UTCDateTime),
        sa.UniqueConstraint("role", "model_key", name="uq_embedding_spaces_key"),
        sa.CheckConstraint("status IN ('filling', 'complete')", name="ck_embedding_spaces_status"),
    )
    op.create_table(
        "embeddings",
        sa.Column(
            "space_id",
            sa.Integer,
            sa.ForeignKey("embedding_spaces.space_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "chunk_id",
            sa.Integer,
            sa.ForeignKey("chunks.chunk_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("vector", sa.LargeBinary, nullable=False),
    )
    op.create_index("ix_embeddings_chunk", "embeddings", ["chunk_id"])


def downgrade() -> None:
    op.drop_index("ix_embeddings_chunk", "embeddings")
    op.drop_table("embeddings")
    op.drop_table("embedding_spaces")
