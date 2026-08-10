"""
Migration: auto-ETA estimation for shipments.

Adds:
  - shipments.transit_days (INTEGER, nullable) — per-shipment override for the business-day
    transit estimate (etd + transit_days). NULL means "use the system default".
  - shipments.eta_source (VARCHAR(10), nullable) — 'AUTO' | 'WEBSITE' | 'MANUAL', tracking who
    last set `eta` so the auto-formula knows when it's safe to recalculate vs. when a human
    or the KPT tracker has already asserted a real value.

Additive / idempotent — safe to run on the live database without touching existing rows.
Run once:
    python backend/scripts/migrations/migrate_shipment_eta_autocalc.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("Adding shipments.transit_days / eta_source ...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS transit_days INTEGER"))
        print("  + transit_days")
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS eta_source VARCHAR(10)"))
        print("  + eta_source")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
