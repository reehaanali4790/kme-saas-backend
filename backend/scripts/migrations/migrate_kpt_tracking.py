"""Add KPT milestone timestamps on shipments for document alert rules."""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text
from config.database import engine


def main():
    with engine.begin() as conn:
        print("shipments.kpt_eta_at / kpt_on_port_at / kpt_departed_at...")
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS kpt_eta_at TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS kpt_on_port_at TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS kpt_departed_at TIMESTAMP"
        ))
    print("KPT tracking columns ready.")


if __name__ == "__main__":
    main()
