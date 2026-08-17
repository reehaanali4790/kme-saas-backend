"""Shared helpers for Alembic revisions — multi-tenant schema-per-org layout."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from config.database import PLATFORM_SCHEMA, SHARED_SCHEMA


def is_multi_tenant(conn: Connection) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_name = 'organizations'"
    ), {"schema": PLATFORM_SCHEMA}).first()
    return row is not None


def list_tenant_schemas(conn: Connection) -> list[str]:
    if is_multi_tenant(conn):
        rows = conn.execute(text(
            "SELECT schema_name FROM platform.organizations "
            "WHERE schema_name IS NOT NULL ORDER BY organization_id"
        )).fetchall()
        if rows:
            return [r[0] for r in rows]
    return ["public"]


def tenant_schemas_with_table(conn: Connection, table_name: str) -> list[str]:
    found: list[str] = []
    for schema in list_tenant_schemas(conn):
        row = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table"
        ), {"schema": schema, "table": table_name}).first()
        if row:
            found.append(schema)
    return found


def set_tenant_search_path(conn: Connection, schema: str) -> None:
    conn.execute(text(
        f'SET search_path TO "{schema}", {SHARED_SCHEMA}, {PLATFORM_SCHEMA}, public'
    ))


def run_sql_on_tenant_schemas(conn: Connection, sql: str, *, label: str = "") -> None:
    """Execute idempotent DDL/DML on every tenant schema that has shipments."""
    schemas = tenant_schemas_with_table(conn, "shipments")
    if not schemas:
        print(f"  WARN: no tenant schemas with shipments — skipped {label or 'tenant DDL'}")
        return
    for schema in schemas:
        set_tenant_search_path(conn, schema)
        conn.execute(text(sql))
        print(f"    applied on {schema}")


def column_exists(conn: Connection, schema: str, table: str, column: str) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
    ), {"schema": schema, "table": table, "column": column}).first()
    return row is not None


def alembic_version_table_exists(conn: Connection) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_name = 'alembic_version'"
    ), {"schema": PLATFORM_SCHEMA}).first()
    return row is not None


def current_alembic_revision(conn: Connection) -> str | None:
    if not alembic_version_table_exists(conn):
        return None
    row = conn.execute(text(
        f"SELECT version_num FROM {PLATFORM_SCHEMA}.alembic_version LIMIT 1"
    )).first()
    return row[0] if row else None
