"""
Migration: Create bill_of_ladings table
Run once: python backend/migrate_bl_table.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import BillOfLading
from sqlalchemy import text

def run_migration():
    print("Creating bill_of_ladings table...")

    Base.metadata.create_all(bind=engine, tables=[BillOfLading.__table__])
    print("  bill_of_ladings table created.")

    # Verify
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'bill_of_ladings'
            ORDER BY ordinal_position
        """))
        cols = result.fetchall()
        print(f"\n  Columns created ({len(cols)}):")
        for col in cols:
            print(f"    {col[0]:30s} {col[1]}")

    print("\nMigration complete.")

if __name__ == "__main__":
    run_migration()
