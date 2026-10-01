"""Alembic environment configured for the application's async engine."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from bot import settings
from database.db import Base
import database.models  # noqa: F401  -- register all model metadata
import database.ledger  # noqa: F401  -- register ledger metadata for autogenerate


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL is intentionally read from the same environment as the bot.
# Do not put a connection string (and especially credentials) into config.ini.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
target_metadata = Base.metadata


def _configure_context(connection=None) -> None:
    """Configure Alembic for PostgreSQL and SQLite-compatible migrations."""
    options = {
        "target_metadata": target_metadata,
        "compare_type": True,
    }
    if connection is not None:
        options["connection"] = connection
        # SQLite needs batch mode for ALTER TABLE operations such as constraints.
        options["render_as_batch"] = connection.dialect.name == "sqlite"
    else:
        options.update(
            {
                "url": settings.DATABASE_URL,
                "literal_binds": True,
                "dialect_opts": {"paramstyle": "named"},
                "render_as_batch": settings.DATABASE_URL.startswith("sqlite"),
            }
        )
    context.configure(**options)


def run_migrations_offline() -> None:
    _configure_context()
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    _configure_context(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations through SQLAlchemy's async driver (asyncpg/aiosqlite)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.DATABASE_URL

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
