"""
Migration: packing line-item grade + verbatim size.
- Add `grade` and `size` text columns to packing_line_items.
Run once: python backend/migrate_packing_line_fields.py  (idempotent — safe to re-run)
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

COLUMNS = [
    ("grade", "VARCHAR(50)"),
    ("size", "VARCHAR(50)"),
]


def run_migration():
    print("Adding grade/size columns to packing_line_items...")
    with engine.begin() as conn:
        for name, ddl in COLUMNS:
            conn.execute(text(
                f"ALTER TABLE packing_line_items ADD COLUMN IF NOT EXISTS {name} {ddl}"
            ))
            print(f"  + {name}")

    with engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'packing_line_items'
              AND column_name IN ('grade', 'size')
        """)).fetchall()
        print(f"\n  packing_line_items new cols present: {sorted({c[0] for c in cols})}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
