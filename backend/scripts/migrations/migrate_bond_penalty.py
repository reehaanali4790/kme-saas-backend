"""
Migration: Into-Bond 180-day penalty tracking.

The 180-day bond clock can be breached: the bonded quantity is still not lifted when
the deadline passes, or the settling Ex-Bond lands after it. Either way a penalty is
payable, and the amount comes off the document or from the user — never computed here.

- goods_declarations.bond_penalty_pkr        amount actually payable
- goods_declarations.bond_penalty_source     DOCUMENT / MANUAL
- goods_declarations.bond_penalty_reason     user reasoning / document reference
- goods_declarations.bond_penalty_days       days late the penalty was assessed for
- goods_declarations.bond_penalty_recorded_at / _by

Run once: python backend/migrate_bond_penalty.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

COLUMNS = [
    ("bond_penalty_pkr", "NUMERIC(15, 2)"),
    ("bond_penalty_source", "VARCHAR(20)"),
    ("bond_penalty_reason", "TEXT"),
    ("bond_penalty_days", "INTEGER"),
    ("bond_penalty_recorded_at", "TIMESTAMP"),
    ("bond_penalty_recorded_by", "INTEGER REFERENCES users(user_id) ON DELETE SET NULL"),
]


def run_migration():
    print("Into-Bond 180-day penalty migration ...")
    with engine.begin() as conn:
        for name, ddl in COLUMNS:
            conn.execute(text(
                f"ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS {name} {ddl}"
            ))
            print(f"  goods_declarations.{name}")

        conn.execute(text(
            "ALTER TABLE goods_declarations DROP CONSTRAINT IF EXISTS valid_bond_penalty_source"
        ))
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD CONSTRAINT valid_bond_penalty_source "
            "CHECK (bond_penalty_source IS NULL OR bond_penalty_source IN ('DOCUMENT','MANUAL'))"
        ))
        print("  valid_bond_penalty_source")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
