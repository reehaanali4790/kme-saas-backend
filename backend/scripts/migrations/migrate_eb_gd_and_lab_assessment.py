"""
Migration: Add m_size to gd_items & ex_bond_items, and lab_report_assessment to goods_declarations.

Run with:
    python backend/scripts/migrations/migrate_eb_gd_and_lab_assessment.py
"""

import sys
import os
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend"))

from config.database import engine, Base
import models.database_models  # ensure models registered

def migrate():
    print("Running migration for EB/GD item details and GD lab report assessment...")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE gd_items ADD COLUMN IF NOT EXISTS m_size VARCHAR(100);"))
        conn.execute(text("ALTER TABLE ex_bond_items ADD COLUMN IF NOT EXISTS m_size VARCHAR(100);"))
        conn.execute(text("ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS lab_report_assessment BOOLEAN DEFAULT FALSE;"))
    print("Migration finished successfully.")

if __name__ == "__main__":
    migrate()
