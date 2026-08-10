"""Trigram search acceleration for the leading-wildcard ilike('%term%') searches used
across the reports, shipments list, and LC table (lc_number, importer_name, vessel_name,
shipment_ref) — a plain B-tree index cannot serve a leading-wildcard pattern at all, so
these columns were previously always sequential-scanned on every search keystroke.

pg_trgm's GIN index lets Postgres use an index scan for ilike('%term%') with NO query
code changes required — the existing ilike() calls benefit automatically once the index
exists. Uses CREATE INDEX CONCURRENTLY for the same reason as migrate_perf_indexes.py
(no ACCESS EXCLUSIVE lock / no blocked writes during the build).
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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


def main():
    # CREATE EXTENSION and CREATE INDEX CONCURRENTLY both need to run outside a
    # transaction block, so use an autocommit connection (same as migrate_perf_indexes.py).
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
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
