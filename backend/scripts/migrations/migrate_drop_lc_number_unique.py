"""Drop legacy UNIQUE(lc_number) from lc_master.

The current model intentionally allows duplicate LC numbers. Databases created
from older SQL bootstraps still carry UNIQUE (lc_number), which blocks LC create
or import when the same number is reused.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from config.database import engine


def run_migration():
    print("Dropping legacy lc_master.lc_number unique constraint (if present)...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE lc_master DROP CONSTRAINT IF EXISTS lc_master_lc_number_key"))
        # Some older dumps used a uniquely named index instead of a constraint.
        conn.execute(text("DROP INDEX IF EXISTS lc_master_lc_number_key"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_lc_master_lc_number "
                "ON lc_master (lc_number)"
            )
        )
    print("Migration complete.")


if __name__ == "__main__":
    run_migration()
