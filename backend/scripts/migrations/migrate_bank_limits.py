"""
Migration: Bank Limit Management (Jul 2026).

Creates the two tables that back the Bank Limit Management feature:
  * bank_limits       — one row per bank/group limit REVISION (validity window + group limit).
  * bank_limit_lines  — company-wise sub-limits under a bank_limits row.

Revisions are new rows (never overwritten), so the report can pick the revision(s)
overlapping a selected date range. Amounts are PKR.

Additive + idempotent (CREATE TABLE IF NOT EXISTS via create_all, which skips existing
tables). Run once:
    python backend/migrate_bank_limits.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import BankLimit, BankLimitLine


def run_migration():
    print("=" * 60)
    print("Bank Limit Management migration")
    print("=" * 60)
    print("Creating tables bank_limits, bank_limit_lines...")
    Base.metadata.create_all(bind=engine, tables=[
        BankLimit.__table__, BankLimitLine.__table__,
    ])
    print("  tables ready.")
    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
