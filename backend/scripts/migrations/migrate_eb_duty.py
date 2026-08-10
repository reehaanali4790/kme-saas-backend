"""
Migration: duty paid per Ex-Bond lifting.

Duty on bonded cargo is paid at the EX-BOND stage, not when it goes into bond. Until now
an ExBondEntry recorded only the quantity lifted, so there was nowhere to put what was
actually paid — which the GD Balance report needs for "GD previously paid".

- ex_bond_entries.duties_paid_pkr    amount paid to clear this lifting
- ex_bond_entries.duty_source        DOCUMENT (read off the Ex-Bond GD) / MANUAL

Run once: python backend/migrate_eb_duty.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("Ex-Bond duty migration ...")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE ex_bond_entries ADD COLUMN IF NOT EXISTS duties_paid_pkr NUMERIC(15, 2)"
        ))
        print("  ex_bond_entries.duties_paid_pkr")

        conn.execute(text(
            "ALTER TABLE ex_bond_entries ADD COLUMN IF NOT EXISTS duty_source VARCHAR(20)"
        ))
        print("  ex_bond_entries.duty_source")

        conn.execute(text(
            "ALTER TABLE ex_bond_entries DROP CONSTRAINT IF EXISTS valid_eb_duty_source"
        ))
        conn.execute(text(
            "ALTER TABLE ex_bond_entries ADD CONSTRAINT valid_eb_duty_source CHECK ("
            "duty_source IS NULL OR duty_source IN ('DOCUMENT','MANUAL'))"
        ))
        print("  valid_eb_duty_source")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
