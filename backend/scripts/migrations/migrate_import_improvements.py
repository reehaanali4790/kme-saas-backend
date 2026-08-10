"""
Migration: import-team improvements (June 2026).
- shipments.lot_number
- financial_instruments table
Run once: python backend/migrate_import_improvements.py  (idempotent)
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import FinancialInstrument
from sqlalchemy import text


def run_migration():
    print("Adding shipments.lot_number...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS lot_number VARCHAR(50)"))
        print("  + lot_number")

    print("\nCreating financial_instruments table...")
    Base.metadata.create_all(bind=engine, tables=[FinancialInstrument.__table__])
    print("  financial_instruments table ready.")

    # Verify
    with engine.connect() as conn:
        has_lot = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='shipments' AND column_name='lot_number'"
        )).first()
        cols = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='financial_instruments'"
        )).scalar()
        print(f"\n  shipments.lot_number present: {bool(has_lot)}")
        print(f"  financial_instruments columns: {cols}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
