"""
Migration: manual upload-date override for Bill of Lading, Commercial Invoice, and
Packing List documents.

Adds upload_date (DATE, nullable) to bill_of_ladings, commercial_invoices, and
packing_lists — a manual override for when the document was actually uploaded/received.
When unset, the UI falls back to the document's created_at (the real system upload
timestamp).

Additive / idempotent — safe to run on the live database without touching existing rows.
Run once:
    python backend/scripts/migrations/migrate_document_upload_dates.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

TABLES = ("bill_of_ladings", "commercial_invoices", "packing_lists")


def run_migration():
    print("Adding upload_date to document tables ...")
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS upload_date DATE"))
            print(f"  + {table}.upload_date")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
