"""
Migration: insurance_certificates table (marine insurance policy/certificate per shipment).

Additive / idempotent — CREATE TABLE only (SQLAlchemy create_all skips it if it exists),
so this is safe to run on the live database without touching existing rows. Run once:
    python backend/migrate_insurance.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import InsuranceCertificate


def run_migration():
    print("Creating insurance_certificates table (if not exists)...")
    Base.metadata.create_all(bind=engine, tables=[InsuranceCertificate.__table__])
    print("  table ready.")
    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
