"""Add LC product shipment and LME tracking columns.

The ORM has long included these fields, but older databases created from the
base schema do not. Add them before any migration loads LCProduct through the
ORM. This migration is additive and idempotent.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from config.database import engine


COLUMNS = [
    ("shipped_quantity", "NUMERIC(12,2) DEFAULT 0"),
    ("pkgs_coils", "INTEGER DEFAULT 0"),
    ("num_containers", "INTEGER DEFAULT 0"),
    ("baseline_lme", "NUMERIC(10,2)"),
    (
        "baseline_bulletin_id",
        "INTEGER REFERENCES lme_bulletins(bulletin_id) ON DELETE SET NULL",
    ),
    ("lme_date_from", "DATE"),
    ("lme_date_to", "DATE"),
    ("lc_lme_difference", "NUMERIC(10,2)"),
    ("current_lme", "NUMERIC(10,2)"),
    ("previous_lme", "NUMERIC(10,2)"),
    ("lme_change_amount", "NUMERIC(10,2)"),
    ("lme_change_percent", "NUMERIC(5,2)"),
]


def run_migration():
    print("Ensuring LC product shipment/LME tracking columns...")
    with engine.begin() as conn:
        for name, ddl in COLUMNS:
            conn.execute(
                text(f"ALTER TABLE lc_products ADD COLUMN IF NOT EXISTS {name} {ddl}")
            )
            print(f"  + lc_products.{name}")

        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_lc_products_baseline_bulletin_id "
                "ON lc_products (baseline_bulletin_id)"
            )
        )
    print("Migration complete.")


if __name__ == "__main__":
    run_migration()
