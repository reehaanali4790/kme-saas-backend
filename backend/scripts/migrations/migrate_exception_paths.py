"""
Migration: Exception paths & documentation intelligence.

SUPERSEDED by Alembic revision ``002_exception_paths`` (run via ``deploy_migrate.py`` or
``alembic upgrade head``). Kept for manual one-off recovery only.

Adds:
  - shipments.contract_id, import_mode, lc_waiver_reason, docs_reception_status
  - file_pending, field_sources on core document tables

Run once (from /app/backend on Railway, or repo root locally):
    PYTHONPATH=. python scripts/migrations/migrate_exception_paths.py
    python backend/scripts/migrations/migrate_exception_paths.py   # from repo root
"""

import os
import sys

# scripts/migrations -> backend package root (/app/backend on Railway)
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from config.database import SHARED_SCHEMA, PLATFORM_SCHEMA, engine
from sqlalchemy import text


def _is_multi_tenant(conn) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'platform' AND table_name = 'organizations'"
    )).first()
    return row is not None


def _tenant_schemas(conn) -> list[str]:
    rows = conn.execute(text(
        "SELECT schema_name FROM platform.organizations "
        "WHERE schema_name IS NOT NULL ORDER BY organization_id"
    )).fetchall()
    return [r[0] for r in rows]


def _has_shipments(conn, schema: str) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_name = 'shipments'"
    ), {"schema": schema}).first()
    return row is not None


def _apply_to_schema(conn, schema: str) -> None:
    conn.execute(text(
        f'SET search_path TO "{schema}", {SHARED_SCHEMA}, {PLATFORM_SCHEMA}, public'
    ))
    print(f"  schema {schema}:")

    conn.execute(text(
        "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS contract_id INTEGER "
        "REFERENCES contracts(contract_id) ON DELETE SET NULL"
    ))
    conn.execute(text(
        "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS import_mode VARCHAR(20) "
        "DEFAULT 'LC_BACKED'"
    ))
    conn.execute(text(
        "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS lc_waiver_reason TEXT"
    ))
    conn.execute(text(
        "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS docs_reception_status VARCHAR(20) "
        "DEFAULT 'NOT_STARTED'"
    ))
    print("    + shipments import path columns")

    for table in (
        "bill_of_ladings",
        "commercial_invoices",
        "packing_lists",
        "financial_instruments",
        "insurance_certificates",
    ):
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
    for table in (
        "bill_of_ladings",
        "commercial_invoices",
        "packing_lists",
        "financial_instruments",
        "insurance_certificates",
    ):
        conn.execute(text(f"""
            UPDATE {table}
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
    print("    + backfilled existing rows")


def run_migration():
    print("Adding exception-path columns ...")
    with engine.begin() as conn:
        if _is_multi_tenant(conn):
            schemas = _tenant_schemas(conn)
            if not schemas:
                print("  WARN: platform.organizations empty — nothing to migrate.")
                return
        else:
            schemas = ["public"]

        migrated = 0
        for schema in schemas:
            if not _has_shipments(conn, schema):
                print(f"  SKIP {schema} — no shipments table")
                continue
            _apply_to_schema(conn, schema)
            migrated += 1

        if migrated == 0:
            print("  WARN: no tenant schemas with shipments were migrated.")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
