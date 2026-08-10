"""Add bill_of_ladings.bl_type (COIL/CONTAINER) and backfill existing rows.

Persists the shipment-type classification produced during BL extraction (see
resolve_bl_type() in modules/shipments/container_detention_service.py) so
downstream logic (alerts, reports, container detention) reads one authoritative
column instead of recomputing a regex guess on every request. Existing rows have
no extraction data to re-run, so they're backfilled with the is_container_bl()
heuristic — the same fallback the resolver itself uses when the AI extractor
returns no confident value.

SAFE / ADDITIVE / IDEMPOTENT — safe to run on every deploy.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from config.database import engine, SessionLocal


def _add_column():
    with engine.begin() as conn:
        print("bill_of_ladings.bl_type...")
        conn.execute(text(
            "ALTER TABLE bill_of_ladings ADD COLUMN IF NOT EXISTS bl_type VARCHAR(20)"
        ))
        conn.execute(text(
            "ALTER TABLE bill_of_ladings DROP CONSTRAINT IF EXISTS valid_bl_type"
        ))
        conn.execute(text("""
            ALTER TABLE bill_of_ladings ADD CONSTRAINT valid_bl_type
            CHECK (bl_type IS NULL OR bl_type IN ('COIL', 'CONTAINER'))
        """))
    print("  bl_type column ready.")


def _backfill():
    from models.database_models import BillOfLading
    from modules.shipments.container_detention_service import is_container_bl

    db = SessionLocal()
    try:
        rows = db.query(BillOfLading).filter(BillOfLading.bl_type.is_(None)).all()
        for bl in rows:
            bl.bl_type = "CONTAINER" if is_container_bl(bl, db) else "COIL"
        db.commit()
        print(f"  backfilled bl_type for {len(rows)} row(s).")
    finally:
        db.close()


def main():
    _add_column()
    _backfill()
    print("Migration complete.")


if __name__ == "__main__":
    main()
