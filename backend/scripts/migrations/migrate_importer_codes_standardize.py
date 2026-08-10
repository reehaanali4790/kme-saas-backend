"""
Migration script: standardize importer short codes to match the requested display
format (see KNOWN_IMPORTER_PROFILES in infrastructure/normalization/normalization_service.py):
  ME -> MEL  (Meen Enterprises / "Meen. (MEL)")
  SC -> SCL  (Steel Craft / "Steel Craft (SCL)")
PCL / MAX / RIL / JMT already matched the requested codes — nothing to change for those.
"""

import sys
import os

backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("Running migration: standardize importer short codes...")
    with engine.connect() as conn:
        res1 = conn.execute(text(
            "UPDATE importers SET short_code = 'MEL' WHERE short_code = 'ME' OR UPPER(name) LIKE '%MEEN%'"
        ))
        res2 = conn.execute(text(
            "UPDATE importers SET short_code = 'SCL' WHERE short_code = 'SC' OR UPPER(name) LIKE '%STEEL CRAFT%'"
        ))
        conn.commit()
        print(f"Updated Meen Enterprises rows (ME -> MEL): {res1.rowcount}")
        print(f"Updated Steel Craft rows (SC -> SCL): {res2.rowcount}")
    print("Migration complete!")


if __name__ == "__main__":
    run_migration()
