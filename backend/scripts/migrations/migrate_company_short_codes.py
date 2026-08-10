"""
Migration script: Update company short codes in `importers` table.
RSI -> RIL (Range Industries)
MCL -> MAX (Max Comfort)
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
    print("Running migration: Update company short codes in importers table...")
    with engine.connect() as conn:
        res1 = conn.execute(text("UPDATE importers SET short_code = 'RIL' WHERE short_code = 'RSI' OR UPPER(name) LIKE '%RANGE%'"))
        res2 = conn.execute(text("UPDATE importers SET short_code = 'MAX' WHERE short_code = 'MCL' OR UPPER(name) LIKE '%MAX COMFORT%'"))
        conn.commit()
        print(f"Updated Range Industries rows: {res1.rowcount}")
        print(f"Updated Max Comfort rows: {res2.rowcount}")
    print("Migration complete!")


if __name__ == "__main__":
    run_migration()
