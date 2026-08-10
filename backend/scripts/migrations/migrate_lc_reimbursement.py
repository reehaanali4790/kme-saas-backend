"""
Migration: LC reimbursement field (+ ensure insurance column exists).

- Add lc_master.reimbursement (W/O | May Add | Confirm — manual select on LC create).
- Add lc_master.insurance IF NOT EXISTS (older DBs may predate it; the LC extractor now
  captures the insurance company name into it).

All additive / idempotent. Run once (safe to re-run):
    python backend/migrate_lc_reimbursement.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

LC_COLUMNS = [
    ("reimbursement", "VARCHAR(20)"),
    ("insurance", "VARCHAR(200)"),
]


def run_migration():
    print("Adding lc_master.reimbursement + ensuring lc_master.insurance...")
    with engine.begin() as conn:
        for name, ddl in LC_COLUMNS:
            conn.execute(text(f"ALTER TABLE lc_master ADD COLUMN IF NOT EXISTS {name} {ddl}"))
            print(f"  + {name}")

    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'lc_master' AND column_name IN ('reimbursement','insurance')
        """)).fetchall()
        print(f"  lc_master cols present: {sorted({c[0] for c in cols})}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
