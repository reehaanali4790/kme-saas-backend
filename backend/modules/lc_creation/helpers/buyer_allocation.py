"""
Buyer / Booked-By allocation — validation + persistence helper.

Shared by the LC create wizard (lc_creation_endpoints) and the LC edit form
(lc_table_endpoints). Turns the form payload into validated LCBuyerAllocation rows.

Payload shape (from APP.mountBuyerAllocation.getValue()):
    {
      "type":  "SINGLE" | "MULTIPLE",
      "basis": "PERCENTAGE" | "QUANTITY" | None,   # only for MULTIPLE
      "rows":  [ {"buyer_name": str, "buyer_id": int|None,
                  "share_percent": num|None, "quantity": num|None}, ... ]
    }

The legacy free-text lc_master.booked_by column is NEVER modified here.
"""

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from models.database_models import LCBuyerAllocation
from infrastructure.normalization.normalization_service import normalize_booked_by

PERCENT_TOL = Decimal("0.10")   # total % may differ from 100 by this much (rounding thirds)
QTY_TOL = Decimal("0.05")       # total qty may differ from LC qty by this much (MT)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _validate_and_build(payload, lc_qty, lc_amount):
    """Return (alloc_type, basis, [row_dict, ...]) or raise HTTPException(400).

    row_dict: {buyer_name, buyer_id, share_percent, quantity, amount} with the auto-computed
    values filled in. lc_qty / lc_amount are Decimals (LC totals) used for the split maths."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid buyer allocation payload")

    alloc_type = str(payload.get("type") or "").strip().upper()
    if alloc_type not in ("SINGLE", "MULTIPLE"):
        raise HTTPException(status_code=400,
                            detail="Buyer allocation type must be SINGLE or MULTIPLE")

    rows_in = payload.get("rows") or []
    # keep only rows with a real buyer name
    rows_in = [r for r in rows_in if isinstance(r, dict) and str(r.get("buyer_name") or "").strip()]
    if not rows_in:
        raise HTTPException(status_code=400, detail="Select at least one buyer for the allocation")

    lc_qty = lc_qty or Decimal("0")
    lc_amount = lc_amount or Decimal("0")

    def _mk(name, buyer_id, share, qty, amt):
        return {
            "buyer_name": str(name).strip()[:300],
            "buyer_id": int(buyer_id) if str(buyer_id or "").strip().isdigit() else None,
            "share_percent": (round(share, 4) if share is not None else None),
            "quantity": (round(qty, 3) if qty is not None else None),
            "amount": (round(amt, 2) if amt is not None else None),
        }

    if alloc_type == "SINGLE":
        if len(rows_in) != 1:
            raise HTTPException(status_code=400,
                                detail="Single-buyer allocation must have exactly one buyer")
        r = rows_in[0]
        return "SINGLE", None, [
            _mk(r.get("buyer_name"), r.get("buyer_id"),
                Decimal("100"), lc_qty, lc_amount)
        ]

    # MULTIPLE
    basis = str(payload.get("basis") or "").strip().upper()
    if basis not in ("PERCENTAGE", "QUANTITY"):
        raise HTTPException(status_code=400,
                            detail="Allocation basis must be PERCENTAGE or QUANTITY")
    if lc_qty <= 0:
        raise HTTPException(status_code=400,
                            detail="Set the LC quantity before allocating to multiple buyers")

    out = []
    if basis == "PERCENTAGE":
        total = Decimal("0")
        for r in rows_in:
            share = _num(r.get("share_percent"))
            if share is None or share <= 0:
                raise HTTPException(status_code=400,
                                    detail=f"Enter a share % for {r.get('buyer_name')}")
            total += share
            qty = (share / Decimal("100")) * lc_qty
            amt = (share / Decimal("100")) * lc_amount
            out.append(_mk(r.get("buyer_name"), r.get("buyer_id"), share, qty, amt))
        if abs(total - Decimal("100")) > PERCENT_TOL:
            raise HTTPException(status_code=400,
                                detail=f"Total allocation is {total}% — it must add up to 100%")
    else:  # QUANTITY
        total = Decimal("0")
        for r in rows_in:
            qty = _num(r.get("quantity"))
            if qty is None or qty <= 0:
                raise HTTPException(status_code=400,
                                    detail=f"Enter a quantity for {r.get('buyer_name')}")
            total += qty
            share = (qty / lc_qty) * Decimal("100")
            amt = (qty / lc_qty) * lc_amount
            out.append(_mk(r.get("buyer_name"), r.get("buyer_id"), share, qty, amt))
        if abs(total - lc_qty) > QTY_TOL:
            raise HTTPException(status_code=400,
                                detail=f"Allocated quantity is {total} MT — it must match the "
                                       f"LC quantity of {lc_qty} MT")

    return "MULTIPLE", basis, out


def apply_allocations(db, lc, payload, lc_qty, lc_amount):
    """Validate `payload` and REPLACE the LC's buyer allocation rows + meta columns.

    Pass payload=None (or missing) to leave allocations untouched. Pass an explicit
    empty/falsey object with no rows only via _validate (which will reject it). To CLEAR
    an allocation, callers pass payload == {} -> handled here as a clear.

    Does not commit — the caller owns the transaction. Never touches lc.booked_by.
    """
    if payload is None:
        return  # not provided -> leave as-is

    # explicit clear: empty dict / no type
    if isinstance(payload, dict) and not payload.get("type"):
        lc.buyer_allocation_type = None
        lc.buyer_allocation_basis = None
        db.query(LCBuyerAllocation).filter(LCBuyerAllocation.lc_id == lc.lc_id).delete()
        return

    alloc_type, basis, rows = _validate_and_build(
        payload, _num(lc_qty), _num(lc_amount))

    for r in rows:
        canon = normalize_booked_by(r["buyer_name"], db)
        if canon:
            r["buyer_name"] = canon

    # replace: delete existing rows, insert fresh
    db.query(LCBuyerAllocation).filter(LCBuyerAllocation.lc_id == lc.lc_id).delete()
    lc.buyer_allocation_type = alloc_type
    lc.buyer_allocation_basis = basis
    for r in rows:
        db.add(LCBuyerAllocation(
            lc_id=lc.lc_id,
            buyer_id=r["buyer_id"],
            buyer_name=r["buyer_name"],
            share_percent=r["share_percent"],
            quantity=r["quantity"],
            amount=r["amount"],
        ))


def serialize_allocations(lc):
    """Return the allocation block for GET responses (edit-form prefill)."""
    rows = sorted(lc.buyer_allocations, key=lambda a: a.alloc_id)
    return {
        "type": lc.buyer_allocation_type,
        "basis": lc.buyer_allocation_basis,
        "rows": [{
            "alloc_id": a.alloc_id,
            "buyer_id": a.buyer_id,
            "buyer_name": a.buyer_name,
            "share_percent": float(a.share_percent) if a.share_percent is not None else None,
            "quantity": float(a.quantity) if a.quantity is not None else None,
            "amount": float(a.amount) if a.amount is not None else None,
        } for a in rows],
    }
