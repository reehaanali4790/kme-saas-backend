"""Add shipments.vessel_status_source / vessel_status_updated_at so the UI can show
whether the current vessel status came from the KPT website scrape or a manual edit."""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from config.database import engine


def main():
    with engine.begin() as conn:
        print("shipments.vessel_status_source / vessel_status_updated_at...")
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS vessel_status_source VARCHAR(10)"
        ))
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS vessel_status_updated_at TIMESTAMP"
        ))
        conn.execute(text("""
            ALTER TABLE shipments DROP CONSTRAINT IF EXISTS valid_vessel_status_source
        """))
        conn.execute(text("""
            ALTER TABLE shipments ADD CONSTRAINT valid_vessel_status_source
            CHECK (vessel_status_source IS NULL OR vessel_status_source IN ('WEBSITE', 'MANUAL'))
        """))
    print("vessel_status_source columns ready.")


if __name__ == "__main__":
    main()
