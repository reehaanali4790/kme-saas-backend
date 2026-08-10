"""
Migration: drop bill_of_ladings.demurrage_per_day.

The per-BL "charge per day" field is dead — compute_demurrage() only ever reads
the global DemurrageConfig.per_day_charge rate plus demurrage_total_amount
(amount actually paid). Mirrors rehan/newchanges' migrate_demurrage_total.py.

Run once: python backend/scripts/migrations/migrate_drop_demurrage_per_day.py  (idempotent)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text

from config.database import engine


def column_exists(conn, table: str, column: str) -> bool:
    r = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return r.scalar() is not None


def main():
    with engine.begin() as conn:
        if column_exists(conn, "bill_of_ladings", "demurrage_per_day"):
            conn.execute(text("ALTER TABLE bill_of_ladings DROP COLUMN demurrage_per_day"))
            print("  Dropped bill_of_ladings.demurrage_per_day")
        else:
            print("  bill_of_ladings.demurrage_per_day already removed")
    print("Done.")


if __name__ == "__main__":
    main()
