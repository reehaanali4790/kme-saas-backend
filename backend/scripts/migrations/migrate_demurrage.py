"""
Migration: demurrage tracking.
- Add demurrage columns to bill_of_ladings.
- Create demurrage_config table and seed the singleton default row.
Run once: python backend/migrate_demurrage.py  (idempotent — safe to re-run)
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import DemurrageConfig
from sqlalchemy import text

BL_COLUMNS = [
    ("demurrage_start_date", "DATE"),
    ("free_days", "INTEGER"),
    ("demurrage_per_day", "NUMERIC(12, 2)"),
    ("demurrage_currency", "VARCHAR(10) DEFAULT 'USD'"),
    ("demurrage_cleared_date", "DATE"),
]


def run_migration():
    print("Adding demurrage columns to bill_of_ladings...")
    with engine.begin() as conn:
        for name, ddl in BL_COLUMNS:
            conn.execute(text(
                f"ALTER TABLE bill_of_ladings ADD COLUMN IF NOT EXISTS {name} {ddl}"
            ))
            print(f"  + {name}")

    print("\nCreating demurrage_config table...")
    Base.metadata.create_all(bind=engine, tables=[DemurrageConfig.__table__])
    print("  demurrage_config table ready.")

    # Seed singleton default row
    with engine.begin() as conn:
        existing = conn.execute(text("SELECT COUNT(*) FROM demurrage_config")).scalar()
        if not existing:
            conn.execute(text(
                "INSERT INTO demurrage_config (free_days, per_day_charge, currency, warn_days) "
                "VALUES (7, 0, 'USD', 3)"
            ))
            print("  Seeded default config (free_days=7, per_day_charge=0, USD, warn_days=3).")
        else:
            print("  Config row already present — left untouched.")

    # Verify
    with engine.connect() as conn:
        bl_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'bill_of_ladings'
              AND column_name LIKE '%demurrage%' OR column_name = 'free_days'
        """)).fetchall()
        cfg = conn.execute(text(
            "SELECT free_days, per_day_charge, currency, warn_days FROM demurrage_config"
        )).fetchall()
        print(f"\n  bill_of_ladings demurrage cols: {sorted({c[0] for c in bl_cols})}")
        print(f"  demurrage_config rows: {cfg}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
