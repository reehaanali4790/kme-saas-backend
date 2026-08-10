"""Add indexes for columns actively range-filtered in dashboard/reports.

Skips on schema-per-tenant deploys — tenant tables live in tenant_* schemas and
ORM indexes are created during tenant provisioning.
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_PRODUCTION_CHECKS", "true")

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


def _is_multi_tenant(conn) -> bool:
    row = conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'platform' AND table_name = 'organizations'
    """)).first()
    return row is not None


def main():
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if _is_multi_tenant(conn):
            print("SKIP perf indexes — multi-tenant platform schema detected.")
            return
        for name, ddl in INDEXES:
            print(f"  {name}...")
            conn.execute(text(ddl))
        print("Performance indexes ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
