from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# `alembic` may be launched from its Scripts directory on Windows, so make the
# repository importable instead of relying on the current executable location.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.database.models import Base

config = context.config
sync_database_url = get_settings().database_url.replace("+asyncpg", "+psycopg").replace("sqlite+aiosqlite", "sqlite")
config.set_main_option("sqlalchemy.url", sync_database_url)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction(): context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # SQLite reports harmless type differences for BigInteger variants used
        # by foreign keys. Column/index/schema differences are still checked.
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_type=connection.dialect.name != 'sqlite',
        )
        with context.begin_transaction(): context.run_migrations()


if context.is_offline_mode(): run_migrations_offline()
else: run_migrations_online()
