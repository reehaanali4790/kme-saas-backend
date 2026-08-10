"""
Migration: name-lookup master tables (importers / suppliers / indentors / booked_by).
Creates the 4 tables and SEEDS them with the distinct names already in the DB so the
searchable dropdowns have data from day one.

Additive / idempotent (re-running won't duplicate — unique on name_norm). Run once:
    python backend/migrate_lookups.py
"""

import sys
import os
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import Importer, Supplier, Indentor, BookedBy
from sqlalchemy import text
from sqlalchemy.orm import Session

# Where each lookup's names already live in the DB.
SOURCES = {
    Importer: [("lc_master", "importer_name"), ("contracts", "buyer_name"),
               ("goods_declarations", "importer_name")],
    Supplier: [("lc_master", "supplier_name"), ("contracts", "supplier_name"),
               ("goods_declarations", "exporter_name")],
    Indentor: [("lc_master", "indentor"), ("contracts", "indentor_name")],
    BookedBy: [("lc_master", "booked_by")],
}


def _norm(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip().upper()
    return s or None


def run_migration():
    print("Creating lookup tables (importers, suppliers, indentors, booked_by)...")
    Base.metadata.create_all(bind=engine, tables=[
        Importer.__table__, Supplier.__table__, Indentor.__table__, BookedBy.__table__,
    ])
    print("  tables ready.")

    print("\nSeeding from existing data...")
    with Session(engine) as db:
        for Model, sources in SOURCES.items():
            seen = {r.name_norm for r in db.query(Model.name_norm).all()}
            collected = {}
            for table, col in sources:
                try:
                    rows = db.execute(text(
                        f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''"
                    )).fetchall()
                except Exception as e:
                    print(f"    skip {table}.{col}: {str(e)[:60]}")
                    continue
                for (val,) in rows:
                    n = _norm(val)
                    if n and n not in seen and n not in collected:
                        collected[n] = str(val).strip()
            # Raw SQL insert (only name + name_norm) — an ORM insert would emit needs_review
            # (Python-side default), which this earlier migration must not reference.
            for n, name in collected.items():
                db.execute(text(
                    f"INSERT INTO {Model.__tablename__} (name, name_norm) VALUES (:name, :norm)"),
                    {"name": name, "norm": n})
            db.commit()
            # Raw SQL count — the ORM count() selects every mapped column in its subquery,
            # which would reference needs_review (added later by migrate_normalization_batch).
            # This migration runs earlier, so that column may not exist yet.
            total = db.execute(text(f"SELECT COUNT(*) FROM {Model.__tablename__}")).scalar()
            print(f"  {Model.__tablename__}: +{len(collected)} (total {total})")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
