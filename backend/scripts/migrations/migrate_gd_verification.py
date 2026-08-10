"""
Migration: goods_declarations verification tracking.

Adds is_verified / verified_at / verified_by (who confirmed the extracted GD data is
correct). These columns are on the GoodsDeclaration ORM model and used by the GD
verify/unverify endpoints, but were never added to the live schema — every query that
loads a GoodsDeclaration (dashboard, reports, arrivals, vessel report, ...) 500s with
"column goods_declarations.is_verified does not exist" until this runs.

Additive / idempotent — safe to run on the live database without touching existing rows.
Run once:
    python backend/scripts/migrations/migrate_gd_verification.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("Adding goods_declarations verification columns ...")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT false"
        ))
        print("  + is_verified")
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP"
        ))
        print("  + verified_at")
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS verified_by INTEGER "
            "REFERENCES users(user_id) ON DELETE SET NULL"
        ))
        print("  + verified_by")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
