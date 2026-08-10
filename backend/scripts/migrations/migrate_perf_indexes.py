"""Add indexes for columns that are actively range-filtered/sorted in the dashboard,
reports, and alert-engine query paths but were missing an index (found during the
data-access performance audit).

Uses CREATE INDEX CONCURRENTLY so this can run against the live database without taking
an ACCESS EXCLUSIVE lock that would block writes — CONCURRENTLY cannot run inside a
transaction block, so this connects with autocommit instead of the usual engine.begin().
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from config.database import engine

INDEXES = [
    ("idx_lcmaster_expiry_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_expiry_date ON lc_master(expiry_date)"),
    ("idx_lcmaster_last_ship_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_last_ship_date ON lc_master(last_ship_date)"),
    ("idx_lcmaster_status_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_status_date ON lc_master(status, lc_date)"),
    ("idx_shipment_eta", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shipment_eta ON shipments(eta)"),
    ("idx_shipment_active_eta",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shipment_active_eta ON shipments(eta) WHERE is_deleted = false"),
    ("idx_gd_filing_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_gd_filing_date ON goods_declarations(filing_date)"),
    ("idx_fi_expiry_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fi_expiry_date ON financial_instruments(expiry_date)"),
    ("idx_bl_demurrage_open",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_bl_demurrage_open ON bill_of_ladings(demurrage_start_date, demurrage_cleared_date) "
     "WHERE demurrage_cleared_date IS NULL"),
]


def main():
    # autocommit: CONCURRENTLY is illegal inside a transaction block
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        for name, ddl in INDEXES:
            print(f"  {name}...")
            conn.execute(text(ddl))
        print("Performance indexes ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
