"""
Migration: add currency_rates.aed_rate column.
Run once: python backend/migrate_aed_rate.py
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import engine
from sqlalchemy import text


def run():
    with engine.connect() as conn:
        exists = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='currency_rates' AND column_name='aed_rate'
        """)).fetchone()
        if exists:
            print("aed_rate already exists.")
        else:
            conn.execute(text("ALTER TABLE currency_rates ADD COLUMN aed_rate NUMERIC(10,4)"))
            conn.commit()
            print("[OK] Added currency_rates.aed_rate")


if __name__ == "__main__":
    run()
