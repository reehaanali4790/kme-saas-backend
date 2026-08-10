"""
Migration: Contract + LC creation module (Steps 1-2).

- Create the contracts and contract_items tables.
- Add lc_master.source and lc_master.contract_id (FK -> contracts) so the new
  contract-driven wizard can link an LC to its contract, while legacy LCs stay
  source='LEGACY_IMPORT'.

All additive / idempotent. Run once (safe to re-run):
    python backend/migrate_contract_lc.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import Contract, ContractItem
from sqlalchemy import text

LC_COLUMNS = [
    ("source", "VARCHAR(20) DEFAULT 'LEGACY_IMPORT'"),
    ("contract_id", "INTEGER REFERENCES contracts(contract_id) ON DELETE SET NULL"),
    ("document_path", "TEXT"),
    ("raw_extracted_data", "JSONB"),
]


def run_migration():
    print("Creating contracts + contract_items tables...")
    # contracts first (contract_items + lc_master.contract_id reference it).
    Base.metadata.create_all(bind=engine, tables=[Contract.__table__, ContractItem.__table__])
    print("  tables ready.")

    print("\nAdding lc_master.source + lc_master.contract_id...")
    with engine.begin() as conn:
        for name, ddl in LC_COLUMNS:
            conn.execute(text(f"ALTER TABLE lc_master ADD COLUMN IF NOT EXISTS {name} {ddl}"))
            print(f"  + {name}")

    # Widen contract free-text columns that real contracts overflow (always safe — only widens).
    print("\nWidening contract free-text columns...")
    with engine.begin() as conn:
        for col, typ in [("quantity_tolerance", "TEXT"), ("payment_terms", "TEXT"),
                         ("delivery_terms", "VARCHAR(120)")]:
            conn.execute(text(f"ALTER TABLE contracts ALTER COLUMN {col} TYPE {typ}"))
            print(f"  ~ {col} -> {typ}")

    # Verify
    with engine.connect() as conn:
        tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('contracts','contract_items')
        """)).fetchall()
        lc_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'lc_master' AND column_name IN ('source','contract_id')
        """)).fetchall()
        print(f"\n  new tables: {sorted({t[0] for t in tables})}")
        print(f"  lc_master new cols: {sorted({c[0] for c in lc_cols})}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
