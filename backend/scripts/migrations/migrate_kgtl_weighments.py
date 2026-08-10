"""
Migration: KGTL weighment reconciliation per GD.

Once a GD is closed the cargo leaves the terminal by truck. Each trip is weighed twice —
on KGTL's weighbridge and on our own — and the two rarely agree exactly. Each row here is
one vehicle trip; the difference (own - KGTL) is derived, never stored, so it cannot drift
away from the weights it comes from.

Weights are held in KG because that is what the weighbridge slips print; the reporting
layer converts to MT to sit alongside the GD's other weights.

Run once: python backend/migrate_kgtl_weighments.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("KGTL weighment reconciliation migration ...")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS gd_kgtl_weighments (
                weighment_id     SERIAL PRIMARY KEY,
                gd_id            INTEGER NOT NULL REFERENCES goods_declarations(gd_id) ON DELETE CASCADE,
                vehicle_number   VARCHAR(50),
                weigh_date       DATE,
                own_weight_kg    NUMERIC(14, 3),
                kgtl_weight_kg   NUMERIC(14, 3),
                remarks          TEXT,
                created_at       TIMESTAMP DEFAULT NOW(),
                created_by       INTEGER REFERENCES users(user_id) ON DELETE SET NULL
            )
        """))
        print("  gd_kgtl_weighments")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_kgtl_gd ON gd_kgtl_weighments(gd_id)"
        ))
        print("  idx_kgtl_gd")

        # Weights are never negative; a blank row (both weights null) is allowed while the
        # user is still collecting slips.
        conn.execute(text(
            "ALTER TABLE gd_kgtl_weighments DROP CONSTRAINT IF EXISTS valid_kgtl_weights"
        ))
        conn.execute(text(
            "ALTER TABLE gd_kgtl_weighments ADD CONSTRAINT valid_kgtl_weights CHECK ("
            "(own_weight_kg IS NULL OR own_weight_kg >= 0) AND "
            "(kgtl_weight_kg IS NULL OR kgtl_weight_kg >= 0))"
        ))
        print("  valid_kgtl_weights")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
