"""
Migration: GD per-item detail.

- Create the gd_items table (one row per GD item: HS, description, valuation, levies).
- Add goods_declarations.levy_breakdown (JSONB) for the GD-level levy totals (incl. WSc).

All additive / idempotent — safe for existing rows. Run once:
    python backend/migrate_gd_items.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import GDItem
from sqlalchemy import text


def run_migration():
    print("Adding goods_declarations.levy_breakdown (JSONB)...")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS levy_breakdown JSONB"
        ))
        print("  + levy_breakdown")

    print("\nCreating gd_items table (if not exists)...")
    Base.metadata.create_all(bind=engine, tables=[GDItem.__table__])
    print("  table ready.")

    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'gd_items'"
        )).fetchall()
        print(f"\n  gd_items present: {bool(tables)}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
