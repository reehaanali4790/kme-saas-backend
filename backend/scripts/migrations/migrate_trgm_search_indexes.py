"""Trigram search acceleration for leading-wildcard ilike('%term%') searches.

On schema-per-tenant deploys, lc_master lives in tenant_* schemas and trigram
indexes are created by the ORM (see LCMaster.__table_args__). This script only
runs for legacy single-schema (public) databases.
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_PRODUCTION_CHECKS", "true")

from sqlalchemy import text
from config.database import engine

INDEXES = [
    ("idx_lcmaster_lc_number_trgm",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_lc_number_trgm "
     "ON lc_master USING gin (lc_number gin_trgm_ops)"),
    ("idx_lcmaster_importer_trgm",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_importer_trgm "
     "ON lc_master USING gin (importer_name gin_trgm_ops)"),
    ("idx_lcmaster_vessel_trgm",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lcmaster_vessel_trgm "
     "ON lc_master USING gin (vessel_name gin_trgm_ops)"),
    ("idx_shipment_vessel_trgm",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shipment_vessel_trgm "
     "ON shipments USING gin (vessel_name gin_trgm_ops)"),
    ("idx_shipment_ref_trgm",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_shipment_ref_trgm "
     "ON shipments USING gin (shipment_ref gin_trgm_ops)"),
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
            print("SKIP trgm indexes — multi-tenant platform schema detected.")
            return

        public_lc = conn.execute(text("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'lc_master'
        """)).first()
        if not public_lc:
            print("SKIP trgm indexes — lc_master not in public.")
            return

        print("  pg_trgm extension...")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for name, ddl in INDEXES:
            print(f"  {name}...")
            conn.execute(text(ddl))
        print("Trigram search indexes ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
