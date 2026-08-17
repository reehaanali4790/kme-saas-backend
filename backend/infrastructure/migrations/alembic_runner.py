"""Programmatic Alembic upgrade/stamp for deploy hooks and provisioning."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from config.database import engine
from infrastructure.migrations.helpers import (
    alembic_version_table_exists,
    column_exists,
    current_alembic_revision,
    tenant_schemas_with_table,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _migration_env(base_env: dict | None = None) -> dict:
    env = dict(base_env or os.environ)
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env.setdefault("ENVIRONMENT", "development")
    env.setdefault("DEBUG", "false")
    env.setdefault("ALLOWED_ORIGINS", "http://localhost")
    env.setdefault("SKIP_PRODUCTION_CHECKS", "true")
    return env


def _run_alembic(*args: str, env: dict | None = None) -> None:
    cmd = [sys.executable, "-m", "alembic", *args]
    result = subprocess.run(cmd, cwd=str(BACKEND_ROOT), env=_migration_env(env))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def maybe_stamp_existing_database() -> None:
    """One-time bridge for DBs that predated Alembic (legacy deploy_migrate scripts)."""
    with engine.connect() as conn:
        if alembic_version_table_exists(conn):
            rev = current_alembic_revision(conn)
            print(f"Alembic already initialized at revision {rev!r}.")
            return

        schemas = tenant_schemas_with_table(conn, "shipments")
        if not schemas:
            print("Alembic: no tenant shipments table yet — fresh DB will migrate normally.")
            return

        sample_schema = schemas[0]
        if column_exists(conn, sample_schema, "shipments", "import_mode"):
            print("Alembic: legacy DB detected (import_mode present) — stamping head.")
            target = "head"
        else:
            print("Alembic: legacy DB detected — stamping 001_baseline before upgrade.")
            target = "001_baseline"

    _run_alembic("stamp", target)


def upgrade_head(env: dict | None = None) -> None:
    print("\n--- Alembic upgrade head ---")
    _run_alembic("upgrade", "head", env=env)


def stamp_head_for_new_tenant(schema_name: str) -> None:
    """New tenants are created via ORM create_all at current head — no replay needed."""
    with engine.begin() as conn:
        if not alembic_version_table_exists(conn):
            return
        row = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = 'shipments'"
        ), {"schema": schema_name}).first()
        if row:
            print(f"Alembic: tenant {schema_name} provisioned (schema matches ORM head).")
