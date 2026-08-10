"""
Migration:
  1. GD status pipeline change — drop "PAID", rename "OUT_OF_CHARGE" -> "RELEASED".
     Existing rows: PAID -> ASSESSED (conservative; don't falsely mark released),
                    OUT_OF_CHARGE -> RELEASED.
     Then replace the valid_gd_status CHECK constraint with the new value set.
  2. shipments.vessel_location — manual free-text vessel location/status field.

All additive / idempotent. Run once (safe to re-run):
    python backend/migrate_gd_status_and_vessel_loc.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

NEW_GD_STATUSES = "'DRAFT','FILED','EXAMINATION','ASSESSED','RELEASED','INTO_BOND','CLEARED','DISPUTED'"


def run_migration():
    with engine.begin() as conn:
        print("1) GD status: migrating existing rows + swapping CHECK constraint...")
        # Drop the old constraint first so the UPDATEs can't trip an intermediate state.
        conn.execute(text("ALTER TABLE goods_declarations DROP CONSTRAINT IF EXISTS valid_gd_status"))
        r1 = conn.execute(text("UPDATE goods_declarations SET status='ASSESSED' WHERE status='PAID'"))
        r2 = conn.execute(text("UPDATE goods_declarations SET status='RELEASED' WHERE status='OUT_OF_CHARGE'"))
        print(f"   PAID->ASSESSED: {r1.rowcount} row(s); OUT_OF_CHARGE->RELEASED: {r2.rowcount} row(s)")
        conn.execute(text(
            f"ALTER TABLE goods_declarations ADD CONSTRAINT valid_gd_status "
            f"CHECK (status IN ({NEW_GD_STATUSES}))"))
        print("   constraint valid_gd_status updated.")

        print("\n2) shipments.vessel_location...")
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS vessel_location VARCHAR(200)"))
        print("   + vessel_location")

        print("\n3) goods_declarations.assessment_report...")
        conn.execute(text("ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS assessment_report TEXT"))
        print("   + assessment_report")

    with engine.connect() as conn:
        leftover = conn.execute(text(
            "SELECT COUNT(*) FROM goods_declarations WHERE status IN ('PAID','OUT_OF_CHARGE')")).scalar()
        has_col = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='shipments' AND column_name='vessel_location'")).scalar()
        print(f"\n  GD rows still PAID/OUT_OF_CHARGE: {leftover} (expect 0)")
        print(f"  shipments.vessel_location present: {bool(has_col)}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
