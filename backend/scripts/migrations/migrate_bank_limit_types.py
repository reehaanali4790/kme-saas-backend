"""
Migration: Bank Limit — LC Type + Bank Limit Type + Parent/Child (Jul 2026).

Extends the Bank Limit Management tables so a limit records WHAT KIND of limit it is
(Regular / One-Off / Temporary) and WHICH LC tenors it covers (Sight / DA / Both), and so
each company sub-limit line carries its own LC tenor. Also widens the line limit_type CHECK
to allow the clearer 'CHILD' value alongside the legacy 'SUB'.

  * bank_limits.bank_limit_type   VARCHAR(12)  -- REGULAR | ONE_OFF | TEMPORARY
  * bank_limits.lc_type           VARCHAR(10)  -- SIGHT | DA | BOTH
  * bank_limit_lines.lc_type      VARCHAR(10)  -- SIGHT | DA | BOTH
  * bank_limit_lines.limit_type CHECK -> allow PARENT / SUB / CHILD

Existing rows are backfilled to REGULAR / BOTH (unchanged behaviour). No data is removed —
old limit revisions stay as history.

Additive + idempotent (ADD COLUMN IF NOT EXISTS, DROP/ADD CONSTRAINT). Run once:
    python backend/migrate_bank_limit_types.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("=" * 60)
    print("Bank Limit — LC Type / Bank Limit Type / Parent-Child migration")
    print("=" * 60)

    with engine.begin() as conn:
        print("1) bank_limits.bank_limit_type + lc_type...")
        conn.execute(text("ALTER TABLE bank_limits ADD COLUMN IF NOT EXISTS "
                          "bank_limit_type VARCHAR(12) DEFAULT 'REGULAR'"))
        conn.execute(text("ALTER TABLE bank_limits ADD COLUMN IF NOT EXISTS "
                          "lc_type VARCHAR(10) DEFAULT 'BOTH'"))
        # backfill any pre-existing NULLs (older rows created before the columns existed)
        conn.execute(text("UPDATE bank_limits SET bank_limit_type='REGULAR' WHERE bank_limit_type IS NULL"))
        conn.execute(text("UPDATE bank_limits SET lc_type='BOTH' WHERE lc_type IS NULL"))
        print("   done.")

        print("2) bank_limit_lines.lc_type...")
        conn.execute(text("ALTER TABLE bank_limit_lines ADD COLUMN IF NOT EXISTS "
                          "lc_type VARCHAR(10) DEFAULT 'BOTH'"))
        conn.execute(text("UPDATE bank_limit_lines SET lc_type='BOTH' WHERE lc_type IS NULL"))
        print("   done.")

        print("3) widen bank_limit_lines.limit_type CHECK (allow CHILD)...")
        conn.execute(text("ALTER TABLE bank_limit_lines DROP CONSTRAINT IF EXISTS valid_bank_limit_type"))
        conn.execute(text(
            "ALTER TABLE bank_limit_lines ADD CONSTRAINT valid_bank_limit_type "
            "CHECK (limit_type IN ('PARENT','SUB','CHILD'))"))
        print("   done.")

    print("\nMigration complete (existing limits -> REGULAR / BOTH; history preserved).")


if __name__ == "__main__":
    run_migration()
