"""
KGTL weighbridge reconciliation.

Cargo leaves the terminal by truck; every trip is weighed on KGTL's weighbridge and again
on our own. The two rarely agree, and the gap is what the import team reports on.

Sign convention, fixed here once so every caller reads it the same way:

    difference = own - KGTL

  * negative -> we received LESS than KGTL says they released  (a shortage — the usual worry)
  * positive -> we received MORE than KGTL says they released

Nothing is flagged or alerted: this records what the slips say and totals it up. The GD is
already closed by the time these rows exist, so the numbers are for reporting, not control.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models.database_models import GdKgtlWeighment, GoodsDeclaration, Shipment

KG_PER_MT = Decimal("1000")
_WEIGHT_TOL_KG = 1.0   # treat gross-weight deltas within 1 KG as a match


def _f(v):
    return float(v) if v is not None else None


def _kg_to_mt(v: Optional[Decimal]):
    return float(v / KG_PER_MT) if v is not None else None


def row_difference_kg(own: Optional[Decimal], kgtl: Optional[Decimal]) -> Optional[Decimal]:
    """own - KGTL. None unless BOTH weights are in — a difference against a missing
    weight would read as a huge shortage rather than as 'not weighed yet'."""
    if own is None or kgtl is None:
        return None
    return own - kgtl


def _pct(diff: Optional[Decimal], base: Optional[Decimal]) -> Optional[float]:
    """Difference as a % of the KGTL weight (the released quantity is the baseline)."""
    if diff is None or base is None or base == 0:
        return None
    return float((diff / base) * Decimal("100"))


def _sum_coils(rows, attr: str) -> Optional[int]:
    vals = [int(getattr(r, attr)) for r in rows if getattr(r, attr, None) is not None]
    return sum(vals) if vals else None


def _diff(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _diff_status(delta: Optional[float], tol: float = _WEIGHT_TOL_KG) -> Optional[str]:
    if delta is None:
        return None
    if abs(delta) <= tol:
        return "MATCH"
    return "SHORT" if delta < 0 else "EXCESS"


def quantity_reconciliation(gd: GoodsDeclaration, db: Session, kgtl: dict) -> dict:
    """BL vs KGTL vs Own — coil count and gross weight (KG) for the GD View summary."""
    bl_coils, bl_gross_kg = None, None
    if gd.shipment_id:
        ship = (db.query(Shipment)
                  .options(joinedload(Shipment.bill_of_ladings))
                  .filter(Shipment.shipment_id == gd.shipment_id)
                  .first())
        if ship and ship.bill_of_ladings:
            bl = max(ship.bill_of_ladings, key=lambda b: b.bl_id or 0)
            if bl.package_count is not None:
                bl_coils = int(bl.package_count)
            if bl.gross_weight_mt is not None:
                bl_gross_kg = float(bl.gross_weight_mt) * 1000.0
    # Fall back to GD View figures when BL is not on file yet.
    if bl_coils is None and gd.package_count is not None:
        bl_coils = int(gd.package_count)
    if bl_gross_kg is None and gd.gross_weight_mt is not None:
        bl_gross_kg = float(gd.gross_weight_mt) * 1000.0

    kgtl_coils = kgtl.get("total_kgtl_coils")
    own_coils = kgtl.get("total_own_coils")
    kgtl_gross_kg = kgtl.get("total_kgtl_kg")
    own_gross_kg = kgtl.get("total_own_kg")

    def _row(label, coils, gross_kg):
        return {"source": label, "coils": coils, "gross_kg": gross_kg}

    rows = [
        _row("BL", bl_coils, bl_gross_kg),
        _row("KGTL", kgtl_coils, kgtl_gross_kg),
        _row("Own", own_coils, own_gross_kg),
    ]

    diff_kgtl_bl_kg = _diff(kgtl_gross_kg, bl_gross_kg)
    diff_own_bl_kg = _diff(own_gross_kg, bl_gross_kg)
    diff_own_kgtl_kg = _diff(own_gross_kg, kgtl_gross_kg)
    diff_kgtl_bl_coils = _diff(kgtl_coils, bl_coils)
    diff_own_bl_coils = _diff(own_coils, bl_coils)
    diff_own_kgtl_coils = _diff(own_coils, kgtl_coils)

    return {
        "rows": rows,
        "bl": rows[0],
        "kgtl": rows[1],
        "own": rows[2],
        "diff_kgtl_vs_bl": {
            "coils": diff_kgtl_bl_coils,
            "gross_kg": diff_kgtl_bl_kg,
            "coils_status": _diff_status(diff_kgtl_bl_coils, tol=0),
            "gross_kg_status": _diff_status(diff_kgtl_bl_kg),
        },
        "diff_own_vs_bl": {
            "coils": diff_own_bl_coils,
            "gross_kg": diff_own_bl_kg,
            "coils_status": _diff_status(diff_own_bl_coils, tol=0),
            "gross_kg_status": _diff_status(diff_own_bl_kg),
        },
        "diff_own_vs_kgtl": {
            "coils": diff_own_kgtl_coils,
            "gross_kg": diff_own_kgtl_kg,
            "coils_status": _diff_status(diff_own_kgtl_coils, tol=0),
            "gross_kg_status": _diff_status(diff_own_kgtl_kg),
        },
    }


def kgtl_summary(gd: GoodsDeclaration, db: Session) -> dict:
    """Weighments for a GD, their differences, and the totals used for reporting."""
    rows = (db.query(GdKgtlWeighment)
              .filter(GdKgtlWeighment.gd_id == gd.gd_id)
              .order_by(GdKgtlWeighment.weigh_date, GdKgtlWeighment.weighment_id)
              .all())

    entries = []
    total_own = Decimal("0")
    total_kgtl = Decimal("0")
    total_own_coils = 0
    total_kgtl_coils = 0
    complete_own = Decimal("0")   # sums restricted to rows with BOTH weights present
    complete_kgtl = Decimal("0")
    complete_rows = 0            # rows with both weights — the only ones that can be compared
    for r in rows:
        own_kg, kgtl_kg = r.own_weight_kg, r.kgtl_weight_kg
        diff = row_difference_kg(own_kg, kgtl_kg)
        if own_kg is not None:
            total_own += own_kg
        if kgtl_kg is not None:
            total_kgtl += kgtl_kg
        if r.own_coils is not None:
            total_own_coils += r.own_coils
        if r.kgtl_coils is not None:
            total_kgtl_coils += r.kgtl_coils
        if diff is not None and own_kg is not None and kgtl_kg is not None:
            complete_rows += 1
            complete_own += own_kg
            complete_kgtl += kgtl_kg
        entries.append({
            "weighment_id": r.weighment_id,
            "vehicle_number": r.vehicle_number,
            "weigh_date": r.weigh_date.isoformat() if r.weigh_date else None,
            "own_weight_kg": _f(own_kg),
            "kgtl_weight_kg": _f(kgtl_kg),
            "own_coils": r.own_coils,
            "kgtl_coils": r.kgtl_coils,
            "difference_kg": _f(diff),
            "difference_pct": _pct(diff, kgtl_kg),
            "remarks": r.remarks,
        })

    # Totals only compare like with like: a row still missing one side is excluded from
    # the difference (rather than voiding the whole total) so one pending vehicle doesn't
    # blank out the difference for every other vehicle that's already fully weighed.
    total_diff = (complete_own - complete_kgtl) if complete_rows else None
    pending = len(rows) - complete_rows

    # Cross-reference against the GD's own declared weight, so the report can show how the
    # trucked-out total compares with what was declared. Advisory only.
    gd_gross_mt = _f(gd.gross_weight_mt)
    vs_gd_mt = None
    if gd_gross_mt is not None and total_kgtl > 0:
        vs_gd_mt = _kg_to_mt(total_kgtl) - gd_gross_mt

    return {
        "count": len(rows),
        "pending_rows": pending,
        "total_own_kg": _f(total_own) if rows else None,
        "total_kgtl_kg": _f(total_kgtl) if rows else None,
        "total_own_coils": total_own_coils or None,
        "total_kgtl_coils": total_kgtl_coils or None,
        "total_difference_kg": _f(total_diff),
        "total_difference_pct": _pct(total_diff, complete_kgtl if total_diff is not None else None),
        "total_own_mt": _kg_to_mt(total_own) if rows else None,
        "total_kgtl_mt": _kg_to_mt(total_kgtl) if rows else None,
        "total_difference_mt": _kg_to_mt(total_diff) if total_diff is not None else None,
        "gd_gross_weight_mt": gd_gross_mt,
        "kgtl_vs_gd_gross_mt": vs_gd_mt,
        "entries": entries,
    }
