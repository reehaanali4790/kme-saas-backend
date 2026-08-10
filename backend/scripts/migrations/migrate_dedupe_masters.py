"""
Migration: de-duplicate the name-master tables (importers / suppliers).

The masters were seeded from raw data, so one real company has many variant rows
(punctuation / SMC-PVT vs PVT / trailing address), e.g. 'PERFECT CRAFT' had 10 rows.
The searchable lookup dropdowns therefore show every variant.

This collapses rows that share a canonical company_key down to ONE keeper (the cleanest
name; renamed to the preferred clean spelling when the company is a known one) and deletes
the rest. The masters are only used for the dropdowns + fuzzy matching (no FK from data
rows), so deleting duplicates is safe.

Idempotent: after a run each key has a single row, so re-running is a no-op. Safe to run on
every deploy — it also cleans up any new variants added manually via the lookup 'add new'.
Run:  python backend/migrate_dedupe_masters.py
"""

import sys
import os
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from models.database_models import Importer, Supplier
from infrastructure.normalization.normalization_service import company_key
from sqlalchemy.orm import Session

# Preferred clean spellings — a keeper whose key matches one of these is renamed to it.
PREFERRED = [
    "PERFECT CRAFT (SMC-PVT) LTD",
    "MAX COMFORT (PVT) LTD",
    "MEEN ENTERPRISES (SMC-PVT) LTD",
    "RANGE INDUSTRIES (SMC-PVT) LTD",
    "STEEL CRAFT (PVT) LTD",
]
_PREF_BY_KEY = {company_key(n): n for n in PREFERRED}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def _score(r):
    """Lower is a better keeper: short, no address/comma, keeps a (legal) form, clean flag."""
    n = r.name or ""
    s = len(n)
    if "," in n:
        s += 1000                       # trailing address -> strongly avoid
    if n.rstrip().endswith("."):
        s += 3
    if "(" in n and ")" in n:
        s -= 2                           # a tidy "(PVT) LTD" form reads well
    if getattr(r, "needs_review", False):
        s += 1
    return s


def _dedupe_model(db, Model):
    groups = {}
    for r in db.query(Model).all():
        k = company_key(r.name)
        if not k:
            continue
        groups.setdefault(k, []).append(r)

    removed = renamed = 0
    for k, rows in groups.items():
        if len(rows) < 2 and k not in _PREF_BY_KEY:
            continue
        keeper = sorted(rows, key=_score)[0]
        # delete the duplicates first (frees any name_norm we may want for the keeper)
        for r in rows:
            if r is not keeper:
                db.delete(r)
                removed += 1
        db.flush()
        # rename keeper to the preferred clean spelling when known
        pref = _PREF_BY_KEY.get(k)
        if pref and keeper.name != pref:
            keeper.name = pref
            keeper.name_norm = _norm(pref)
            if hasattr(keeper, "needs_review"):
                keeper.needs_review = False
            renamed += 1
    db.commit()
    print(f"  {Model.__tablename__}: removed {removed} duplicate(s), renamed {renamed} keeper(s), "
          f"now {db.query(Model).count()} rows.")


def run_migration():
    print("=" * 60)
    print("De-duplicate name masters (importers / suppliers)")
    print("=" * 60)
    with Session(engine) as db:
        _dedupe_model(db, Importer)
        _dedupe_model(db, Supplier)
    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
