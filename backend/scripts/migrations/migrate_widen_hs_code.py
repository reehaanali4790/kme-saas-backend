"""
Migration: widen hs_code columns from VARCHAR(20) -> VARCHAR(50).

Root cause: invoices (and other docs) sometimes carry MORE THAN ONE HS code, e.g.
"7210.4910, 7210.7010", which is 21 chars and overflowed the old VARCHAR(20),
crashing the save with psycopg2 StringDataRightTruncation. 50 is comfortable.

Additive / idempotent (ALTER ... TYPE is safe to re-run; only widens, never narrows).
Skips any table that doesn't exist yet.

Run once (safe to re-run):
    python backend/migrate_widen_hs_code.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

# tables that carry an hs_code column
TABLES = ["commercial_invoices", "packing_lists", "lc_products",
          "goods_declarations", "contracts"]


def run_migration():
    with engine.begin() as conn:
        for table in TABLES:
            exists = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name='hs_code'"
            ), {"t": table}).scalar()
            if not exists:
                print(f"  SKIP {table} (no hs_code column)")
                continue
            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN hs_code TYPE VARCHAR(50)"))
            print(f"  + {table}.hs_code -> VARCHAR(50)")

    with engine.connect() as conn:
        for table in TABLES:
            length = conn.execute(text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name=:t AND column_name='hs_code'"
            ), {"t": table}).scalar()
            if length is not None:
                print(f"  {table}.hs_code length now: {length}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
