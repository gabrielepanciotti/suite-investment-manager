"""Alembic environment for InvestmentManager.

The DSN comes from `DATABASE_URL` (the node's .env), never from alembic.ini —
a versioned file must not carry credentials. Metadata comes from
`suite_backend_core.Base`, so every model that inherits from it is picked up by
autogenerate as soon as its module is imported below.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from suite_backend_core import Base

# Import the node's models so they register on Base.metadata. A node that does
# not own data yet has no `db` module — that is legitimate, and autogenerate
# simply sees an empty metadata instead of failing to start.
try:
    import investment_manager_backend.db  # noqa: F401
except ImportError:
    pass

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
