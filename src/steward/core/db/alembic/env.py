# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Alembic environment shared by every migration owner (core and each module).

Run only through steward.core.db.migrate, which passes the connection and the owner's
version table in `config.attributes`.
"""

from alembic import context

config = context.config
connection = config.attributes["connection"]

context.configure(
    connection=connection,
    version_table=config.attributes["version_table"],
    transactional_ddl=True,
    render_as_batch=True,
)

with context.begin_transaction():
    context.run_migrations()
