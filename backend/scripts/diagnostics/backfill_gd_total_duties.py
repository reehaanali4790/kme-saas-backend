"""One-off data fix: recompute goods_declarations.total_duties_pkr for existing rows where
it is NULL but the itemized duty/tax/charge components are present.

Root cause: total_duties_pkr was only ever set from a single printed "Total Duties" figure
on the GD View / Into-Bond GD document (see modules/weboc/services.py::apply_gd_view() /
apply_into_bond_gd()). Many real WeBOC GD Views print only itemized duty rows with no
combined total line, so the field was left NULL even though its components
(customs_duty_pkr, sales_tax_pkr, ...) were captured — starving the GD Reporting page's
duty totals ("Potential GD to Pay" / IB value) down to PKR 0. modules/weboc/services.py now
applies this same fallback going forward on new uploads (mirrors the existing
_duty_from_extracted() helper, already used for Ex-Bond entries); this script backfills rows
saved before that fix.

NOT part of deploy_migrate.py — a data fix, not a schema change. Run it manually once:
    cd backend && python scripts/diagnostics/backfill_gd_total_duties.py
Add --dry-run to preview changes without writing.
"""

import os
import sys
from decimal import Decimal

backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from config.database import SessionLocal
from models.database_models import GoodsDeclaration

DUTY_COMPONENT_FIELDS = [
    "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
    "additional_customs_duty_pkr", "additional_sales_tax_pkr",
    "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr",
]


def _computed_total(gd: GoodsDeclaration):
    total = Decimal("0")
    for f in DUTY_COMPONENT_FIELDS:
        part = getattr(gd, f, None)
        if part is not None and part > 0:
            total += part
    return total if total > 0 else None


def run(dry_run: bool = False):
    db = SessionLocal()
    try:
        rows = db.query(GoodsDeclaration).filter(GoodsDeclaration.total_duties_pkr.is_(None)).all()
        changed = 0
        for gd in rows:
            computed = _computed_total(gd)
            if computed is None:
                continue
            print(f"  gd_id={gd.gd_id} gd_number={gd.gd_number!r} gd_type={gd.gd_type!r}: "
                  f"total_duties_pkr NULL -> {computed}")
            changed += 1
            if not dry_run:
                gd.total_duties_pkr = computed
        if not dry_run:
            db.commit()
        print(f"\n{'Would update' if dry_run else 'Updated'} {changed} of {len(rows)} "
              f"NULL total_duties_pkr row(s).")
    finally:
        db.close()


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
