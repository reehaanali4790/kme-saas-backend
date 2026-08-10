"""
Migration: Create system_alerts table (operational alert engine).
Run once: python backend/migrate_system_alerts.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import SystemAlert
from sqlalchemy import text


def run_migration():
    print("Creating system_alerts table...")
    Base.metadata.create_all(bind=engine, tables=[SystemAlert.__table__])
    with engine.connect() as conn:
        cnt = conn.execute(text("""
            SELECT count(*) FROM information_schema.columns WHERE table_name = 'system_alerts'
        """)).scalar()
    print(f"  [OK] system_alerts created ({cnt} columns)")
    print("Migration complete.")


if __name__ == "__main__":
    run_migration()
