"""Alembic environment.

Reads the connection string from `DATABASE_URL` rather than `alembic.ini`, so no
database path is ever committed and the live ledger cannot be targeted by
accident from a checked-in file.

`raseed.config.Settings` is deliberately not used here: it requires a bot token
and an API key, and a migration needs neither.

Brief section 16.1: run migrations against a copy first, never the live file.
The ledger is a single SQLite file with no way to rebuild it, because the images
are deleted after confirm.
"""

from __future__ import annotations

import os
import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# The package is not pip-installed into the venv, so put src on the path the same
# way pytest and mypy do.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from raseed.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DEFAULT_URL = "sqlite:///raseed.db"


def _database_url() -> str:
    """Environment first, then `alembic.ini`, then a local SQLite file."""
    return os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url") or DEFAULT_URL


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to anything."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place, so Alembic rebuilds the
            # table instead. Required for any future column change to work at all.
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
