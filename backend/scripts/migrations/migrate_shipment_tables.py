"""
Migration: Create Shipment Document Hub tables
  - shipments
  - commercial_invoices + invoice_line_items
  - packing_lists + packing_line_items
  - document_validations
  - goods_declarations
Also adds bill_of_ladings.shipment_id column.

Run once: python backend/migrate_shipment_tables.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import (
    Shipment, CommercialInvoice, InvoiceLineItem,
    PackingList, PackingLineItem, DocumentValidation, GoodsDeclaration,
)
from sqlalchemy import text

NEW_TABLES = [
    Shipment.__table__,
    CommercialInvoice.__table__,
    InvoiceLineItem.__table__,
    PackingList.__table__,
    PackingLineItem.__table__,
    DocumentValidation.__table__,
    GoodsDeclaration.__table__,
]


def run_migration():
    print("Creating Shipment Document Hub tables...")
    Base.metadata.create_all(bind=engine, tables=NEW_TABLES)
    for t in NEW_TABLES:
        print(f"  [OK] {t.name}")

    # Add shipment_id to bill_of_ladings if missing
    print("\nLinking bill_of_ladings to shipments...")
    with engine.connect() as conn:
        exists = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'bill_of_ladings' AND column_name = 'shipment_id'
        """)).fetchone()
        if exists:
            print("  bill_of_ladings.shipment_id already exists.")
        else:
            conn.execute(text("""
                ALTER TABLE bill_of_ladings
                ADD COLUMN shipment_id INTEGER REFERENCES shipments(shipment_id) ON DELETE SET NULL
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_bill_of_ladings_shipment_id "
                "ON bill_of_ladings (shipment_id)"
            ))
            conn.commit()
            print("  [OK] Added bill_of_ladings.shipment_id")

    # Report
    with engine.connect() as conn:
        print("\nTable column counts:")
        for t in NEW_TABLES:
            cnt = conn.execute(text("""
                SELECT count(*) FROM information_schema.columns WHERE table_name = :tn
            """), {"tn": t.name}).scalar()
            print(f"  {t.name:24s} {cnt} columns")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
