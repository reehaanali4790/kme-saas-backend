"""
Migration: ensure lc_amount lives on lc_products (per product line).

SAFE / ADDITIVE / IDEMPOTENT — replaces the old destructive version:
- Adds lc_products.lc_amount if missing.
- Backfills it from the legacy lc_master.lc_amount when that column still exists
  (older prod schema stored the amount on lc_master).
- Does NOT drop lc_master.lc_amount, so no data is lost and re-running is safe.

Run: python backend/migrate_lc_amount.py
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import engine
from sqlalchemy import text


def run():
    print("Ensuring lc_products.lc_amount...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE lc_products ADD COLUMN IF NOT EXISTS lc_amount NUMERIC(15,2)"))
        print("  + lc_products.lc_amount")

        legacy = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'lc_master' AND column_name = 'lc_amount'
        """)).fetchone()
        if legacy:
            res = conn.execute(text("""
                UPDATE lc_products lp
                SET lc_amount = lm.lc_amount
                FROM lc_master lm
                WHERE lp.lc_id = lm.lc_id
                  AND lp.lc_amount IS NULL
                  AND lm.lc_amount IS NOT NULL
            """))
            print(f"  backfilled lc_amount from lc_master ({res.rowcount} row(s))")
        else:
            print("  no legacy lc_master.lc_amount column — nothing to backfill")

    print("Migration complete.")


if __name__ == '__main__':
    run()
