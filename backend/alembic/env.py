"""Alembic environment — multi-tenant schema-per-org (platform / shared / tenant_*)."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# backend/ package root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import (  # noqa: E402
    PLATFORM_SCHEMA,
    SHARED_SCHEMA,
    Base,
    resolve_database_url,
)
import models.database_models  # noqa: F401, E402 — register tenant ORM
import models.platform_models  # noqa: F401, E402 — register platform/shared ORM

config = context.config
config.set_main_option("sqlalchemy.url", resolve_database_url())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    """Autogenerate only platform + shared tables; tenant DDL lives in revision scripts."""
    if type_ == "table":
        schema = getattr(object_, "schema", None)
        if schema in (PLATFORM_SCHEMA, SHARED_SCHEMA):
            return True
        if schema is None:
            return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema=PLATFORM_SCHEMA,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=PLATFORM_SCHEMA,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
