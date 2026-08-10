"""
Migration: shipment delivery date + DELIVERED status.

- Add shipments.delivery_date (DATE). Setting it marks the shipment delivered/closed.
- Widen the valid_shipment_status CHECK constraint to include 'DELIVERED'.

Additive / idempotent — safe for existing rows. Run once:
    python backend/migrate_delivery_date.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

# Full superset — new automatic codes + every legacy code. Kept identical across all
# migrations that (re)build valid_shipment_status so repeat deploys never reject a row,
# regardless of which migration runs last. See migrate_shipment_status_gd_weboc.py.
STATUS_VALUES = ("'PENDING','COPY_DOCS_RECEIVED','DOCS_AT_BANK','PAYMENT_RECEIVED',"
                 "'ORIGINAL_DOCS_RECEIVED','DELIVERED',"
                 "'DOCUMENTS_RECEIVED','VALIDATED','DISCREPANT','BANK_PRESENTATION',"
                 "'ACCEPTED','PAYMENT_MADE','CLOSED'")


def run_migration():
    print("Adding shipments.delivery_date ...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivery_date DATE"))
        print("  + delivery_date")

    print("\nWidening valid_shipment_status to include DELIVERED ...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shipments DROP CONSTRAINT IF EXISTS valid_shipment_status"))
        conn.execute(text(
            f"ALTER TABLE shipments ADD CONSTRAINT valid_shipment_status "
            f"CHECK (status IN ({STATUS_VALUES}))"
        ))
        print("  valid_shipment_status updated")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
