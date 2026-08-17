"""
Migration: Exception paths & documentation intelligence.

Adds:
  - shipments.contract_id, import_mode, lc_waiver_reason, docs_reception_status
  - file_pending, field_sources on core document tables

Run once:
    python backend/scripts/migrations/migrate_exception_paths.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("Adding exception-path columns ...")
    with engine.begin() as conn:
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
        print("  + shipments import path columns")

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
            print(f"  + {table}.file_pending / field_sources")

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
        conn.execute(text("""
            UPDATE bill_of_ladings
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
        conn.execute(text("""
            UPDATE commercial_invoices
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
        conn.execute(text("""
            UPDATE packing_lists
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
        conn.execute(text("""
            UPDATE financial_instruments
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
        conn.execute(text("""
            UPDATE insurance_certificates
            SET file_pending = (document_path IS NULL OR document_path = '')
            WHERE file_pending IS NULL
        """))
        print("  + backfilled existing rows")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
