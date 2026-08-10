"""
Migration: Buyer / Booked-By Allocation (Jul 2026).

Adds structured buyer allocation to LCs (clean, reportable buyer splits) WITHOUT touching
the existing free-text lc_master.booked_by column — legacy data stays exactly as it is.

  * lc_master.buyer_allocation_type   VARCHAR(10)  -- 'SINGLE' | 'MULTIPLE' | NULL(legacy)
  * lc_master.buyer_allocation_basis  VARCHAR(12)  -- 'PERCENTAGE' | 'QUANTITY' | NULL
  * lc_buyer_allocations              -- one row per buyer (share % / qty / amount)

No backfill: existing LCs keep NULL allocation type and continue to use booked_by text.
Structured allocation is captured going forward from the LC Create/Edit form.

Additive + idempotent (ADD COLUMN IF NOT EXISTS + create_all for the new table). Run once:
    python backend/migrate_buyer_allocation.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import LCBuyerAllocation
from sqlalchemy import text


def run_migration():
    print("=" * 60)
    print("Buyer / Booked-By Allocation migration")
    print("=" * 60)

    with engine.begin() as conn:
        print("1) lc_master allocation meta columns...")
        conn.execute(text("ALTER TABLE lc_master ADD COLUMN IF NOT EXISTS "
                          "buyer_allocation_type VARCHAR(10)"))
        conn.execute(text("ALTER TABLE lc_master ADD COLUMN IF NOT EXISTS "
                          "buyer_allocation_basis VARCHAR(12)"))
        print("   done.")

    print("2) lc_buyer_allocations table...")
    Base.metadata.create_all(bind=engine, tables=[LCBuyerAllocation.__table__])
    print("   done.")

    print("\nMigration complete (legacy booked_by text untouched).")


if __name__ == "__main__":
    run_migration()
