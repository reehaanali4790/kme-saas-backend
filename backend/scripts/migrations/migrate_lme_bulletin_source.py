"""Add lme_bulletins.source ('MANUAL' | 'WEB') so a manually-uploaded bulletin and an
auto-synced thehelpers.pk bulletin can coexist for the same bulletin_date instead of
one silently overwriting the other. The lme_sync_runs table (execution history for the
web-sync job) is a brand new table, so it's created by Base.metadata.create_all() at
startup like every other new model - no DDL needed for it here."""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from config.database import engine


def main():
    with engine.begin() as conn:
        print("lme_bulletins.source...")
        conn.execute(text(
            "ALTER TABLE lme_bulletins ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'MANUAL'"
        ))
        conn.execute(text(
            "ALTER TABLE lme_bulletins DROP CONSTRAINT IF EXISTS uq_bulletin_date_source"
        ))
        conn.execute(text(
            "ALTER TABLE lme_bulletins ADD CONSTRAINT uq_bulletin_date_source UNIQUE (bulletin_date, source)"
        ))
    print("lme_bulletins.source ready.")


if __name__ == "__main__":
    main()
