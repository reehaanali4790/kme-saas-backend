"""
One-off data cleanup: strip leaked quantity/price/Incoterm text out of already-stored
lc_products.product_name values (see clean_goods_description() in
infrastructure/normalization/normalization_service.py — the LC extractor used to store
the raw SWIFT F45A block verbatim, e.g. "5000 MT COLD ROLLED COILS SECONDARY QUALITY AT
USD 593.00 PER M/TON CFR KARACHI", instead of just the commodity description).

This is NOT part of deploy_migrate.py — it's a data fix, not a schema change. Run it
manually once after the extraction-prompt fix ships:
    cd backend && python scripts/migrations/backfill_clean_product_names.py
Add --dry-run to preview changes without writing.
"""

import sys
import os

backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from config.database import SessionLocal
from models.database_models import LCProduct
from infrastructure.normalization.normalization_service import clean_goods_description


def run(dry_run: bool = False):
    db = SessionLocal()
    try:
        rows = db.query(LCProduct).filter(LCProduct.product_name.isnot(None)).all()
        changed = 0
        for p in rows:
            cleaned = clean_goods_description(p.product_name)
            if cleaned and cleaned != p.product_name:
                print(f"  line_id={p.line_id}: {p.product_name!r} -> {cleaned!r}")
                changed += 1
                if not dry_run:
                    p.product_name = cleaned
        if not dry_run:
            db.commit()
        print(f"\n{'Would update' if dry_run else 'Updated'} {changed} of {len(rows)} product_name rows.")
    finally:
        db.close()


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
