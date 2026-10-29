# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: the content index (texts, chunks and their full-text index).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "texts",
        sa.Column(
            "content_hash",
            sa.Text,
            sa.ForeignKey("blobs.content_hash", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("artifact", sa.Text, primary_key=True),
        sa.Column("part", sa.Integer, primary_key=True),
        sa.Column("page", sa.Integer),
        sa.Column("heading", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "content_hash",
            sa.Text,
            sa.ForeignKey("blobs.content_hash", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("page", sa.Integer),
        sa.Column("heading", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint("content_hash", "artifact", "seq", name="uq_chunks_position"),
    )
    # External-content FTS5 index over chunks, kept in sync by triggers. Accents are
    # ignored (`remove_diacritics 2`), case is folded by the tokenizer.
    op.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "heading, body, content='chunks', content_rowid='chunk_id', "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    # `rank` = BM25 with headings weighted twice the body (usable inside aggregates,
    # which the bm25() function isn't).
    op.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('rank', 'bm25(2.0, 1.0)')")
    op.execute(
        "CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN "
        "INSERT INTO chunks_fts(rowid, heading, body) "
        "VALUES (new.chunk_id, coalesce(new.heading, ''), new.body); END"
    )
    op.execute(
        "CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN "
        "INSERT INTO chunks_fts(chunks_fts, rowid, heading, body) "
        "VALUES ('delete', old.chunk_id, coalesce(old.heading, ''), old.body); END"
    )
    op.execute(
        "CREATE TRIGGER chunks_fts_update AFTER UPDATE ON chunks BEGIN "
        "INSERT INTO chunks_fts(chunks_fts, rowid, heading, body) "
        "VALUES ('delete', old.chunk_id, coalesce(old.heading, ''), old.body); "
        "INSERT INTO chunks_fts(rowid, heading, body) "
        "VALUES (new.chunk_id, coalesce(new.heading, ''), new.body); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER chunks_fts_update")
    op.execute("DROP TRIGGER chunks_fts_delete")
    op.execute("DROP TRIGGER chunks_fts_insert")
    op.execute("DROP TABLE chunks_fts")
    op.drop_table("chunks")
    op.drop_table("texts")
