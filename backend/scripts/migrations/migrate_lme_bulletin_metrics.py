"""Add current LME bulletin extraction/result columns.

Older schemas use a different set of processing-status columns. Keep those
legacy columns and add the fields required by the current ORM and APIs.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from config.database import engine


def run_migration():
    print("Ensuring current LME bulletin columns...")
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE lme_bulletins "
                "ADD COLUMN IF NOT EXISTS rate_id INTEGER "
                "REFERENCES currency_rates(rate_id) ON DELETE SET NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE lme_bulletins "
                "ADD COLUMN IF NOT EXISTS symbols_extracted INTEGER DEFAULT 0"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE lme_bulletins "
                "ADD COLUMN IF NOT EXISTS prices_stored INTEGER DEFAULT 0"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_lme_bulletins_rate_id "
                "ON lme_bulletins (rate_id)"
            )
        )
    print("Migration complete.")


if __name__ == "__main__":
    run_migration()
