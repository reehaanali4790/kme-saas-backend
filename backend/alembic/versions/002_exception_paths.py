"""Exception paths & documentation intelligence (tenant DDL).

Revision ID: 002_exception_paths
Revises: 001_baseline
Create Date: 2026-08-17

Adds import-mode columns on shipments and file_pending/field_sources on core document
tables. Idempotent — safe on every deploy and for legacy DBs stamped at 001_baseline.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from infrastructure.migrations.helpers import (
    set_tenant_search_path,
    tenant_schemas_with_table,
)

revision: str = "002_exception_paths"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SHIPMENT_DDL = """
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS contract_id INTEGER
    REFERENCES contracts(contract_id) ON DELETE SET NULL;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS import_mode VARCHAR(20) DEFAULT 'LC_BACKED';
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS lc_waiver_reason TEXT;
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS docs_reception_status VARCHAR(20) DEFAULT 'NOT_STARTED';
"""

_DOC_TABLES = (
    "bill_of_ladings",
    "commercial_invoices",
    "packing_lists",
    "financial_instruments",
    "insurance_certificates",
)


def _apply_tenant_schema(conn, schema: str) -> None:
    set_tenant_search_path(conn, schema)
    print(f"  schema {schema}:")

    for stmt in _SHIPMENT_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(text(stmt))
    print("    + shipments import path columns")

    for table in _DOC_TABLES:
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS file_pending BOOLEAN DEFAULT FALSE"
        ))
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS field_sources JSONB"
        ))
        print(f"    + {table}.file_pending / field_sources")

    conn.execute(text("""
        UPDATE shipments s
        SET contract_id = l.contract_id
        FROM lc_master l
        WHERE s.lc_id = l.lc_id AND s.contract_id IS NULL
    """))
    conn.execute(text(
        "UPDATE shipments SET import_mode = 'LC_BACKED' WHERE import_mode IS NULL"
    ))
    conn.execute(text(
        "UPDATE shipments SET docs_reception_status = 'NOT_STARTED' "
        "WHERE docs_reception_status IS NULL"
    ))
    for table in _DOC_TABLES:
        conn.execute(text(f"""
            UPDATE {table}
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
    print("    + backfilled existing rows")


def upgrade() -> None:
    print("Alembic 002: exception-path columns ...")
    conn = op.get_bind()
    schemas = tenant_schemas_with_table(conn, "shipments")
    if not schemas:
        print("  WARN: no tenant schemas with shipments — skipped.")
        return

    migrated = 0
    for schema in schemas:
        _apply_tenant_schema(conn, schema)
        migrated += 1

    if migrated == 0:
        print("  WARN: no tenant schemas were migrated.")


def downgrade() -> None:
    conn = op.get_bind()
    for schema in tenant_schemas_with_table(conn, "shipments"):
        set_tenant_search_path(conn, schema)
        conn.execute(text("ALTER TABLE shipments DROP COLUMN IF EXISTS contract_id"))
        conn.execute(text("ALTER TABLE shipments DROP COLUMN IF EXISTS import_mode"))
        conn.execute(text("ALTER TABLE shipments DROP COLUMN IF EXISTS lc_waiver_reason"))
        conn.execute(text("ALTER TABLE shipments DROP COLUMN IF EXISTS docs_reception_status"))
        for table in _DOC_TABLES:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS file_pending"))
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS field_sources"))
