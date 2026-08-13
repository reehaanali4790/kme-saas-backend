"""
LME Monitoring System - LC Table API (IMPROVED)
With formula determination, no supplier/quantity/amount
Version: 2.0.0
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tenant import get_tenant_db
from models.database_models import User, LCMaster, LCProduct, BillOfLading
from modules.auth.dependencies import get_current_user
from modules.auth.services import AuthService
from infrastructure.formula_engine.formula_engine import FormulaEngine
from modules.lc_creation.helpers.buyer_allocation import apply_allocations, serialize_allocations
from infrastructure.normalization.normalization_service import normalize_lc_master, detect_quality
from modules.shipments import journey_service as journey_svc

router = APIRouter(prefix="/api/lc-table", tags=["LC Table"])


def determine_formula(product_code, origin, quality):
    """
    Determine which formula to use for LME calculation
    Uses FormulaEngine as single source of truth
    """
    return FormulaEngine.determine_formula(product_code, origin, quality)


@router.get("/list")
def get_lc_table(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None),
    status: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    product_code: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    origin: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    quality: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    size: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("lc_date"),
    sort_order: str = Query("desc"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """
    Get LC table - IMPROVED VERSION
    Removed: supplier_name, quantity, lc_amount
    Added: formula_id
    """

    # Build base query with JOIN
    query = db.query(
        LCMaster.lc_id,
        LCMaster.lc_number,
        LCMaster.lc_date,
        LCMaster.expiry_date,
        LCMaster.last_ship_date,
        LCMaster.monitoring_expiry,
        LCMaster.currency,
        LCMaster.status,
        LCMaster.contract_number,
        LCMaster.supplier_name,
        LCMaster.importer_name,
        LCMaster.hoa,
        LCMaster.payment_terms,
        LCProduct.line_id,
        LCProduct.product_code,
        LCProduct.product_name,
        LCProduct.item_review,
        LCProduct.origin,
        LCProduct.quality,
        LCProduct.lc_amount,
        LCProduct.lc_unit_price,
        LCProduct.quantity,
        LCProduct.unit,
        LCProduct.hs_code,
        LCProduct.grade,
        LCProduct.size,
        LCProduct.cargo_nature,
        LCProduct.baseline_lme,
        LCProduct.current_lme,
        LCProduct.lme_change_amount,
        LCProduct.lme_change_percent,
        LCProduct.lme_date_from,
        LCProduct.lme_date_to,
        LCProduct.lc_lme_difference,
        LCProduct.last_lme_update,
    ).join(
        LCProduct, LCMaster.lc_id == LCProduct.lc_id
    )

    # Apply filters
    filters = []

    # Search filter
    if search:
        search_term = f"%{search}%"
        filters.append(
            or_(
                LCMaster.lc_number.ilike(search_term),
                LCProduct.product_code.ilike(search_term),
                LCProduct.product_name.ilike(search_term)
            )
        )

    # Status filter (OR'd when multiple)
    status_vals = []
    if status:
        for s in status:
            if s and s != "ALL":
                for part in s.split(","):
                    p = part.strip()
                    if p:
                        status_vals.append(p)
    if status_vals:
        filters.append(LCMaster.status.in_(status_vals))

    # Product filter (OR'd when multiple)
    product_vals = []
    if product_code:
        for p in product_code:
            if p and p != "ALL":
                for part in p.split(","):
                    pt = part.strip()
                    if pt:
                        product_vals.append(pt)
    if product_vals:
        filters.append(LCProduct.product_code.in_(product_vals))

    # Origin filter (OR'd when multiple)
    origin_vals = []
    if origin:
        for o in origin:
            if o and o != "ALL":
                for part in o.split(","):
                    ot = part.strip()
                    if ot:
                        origin_vals.append(ot)
    if origin_vals:
        filters.append(LCProduct.origin.in_(origin_vals))

    # Quality filter (OR'd when multiple)
    quality_vals = []
    if quality:
        for q in quality:
            if q and q != "ALL":
                for part in q.split(","):
                    qt = part.strip()
                    if qt:
                        quality_vals.append(qt)
    if quality_vals:
        filters.append(LCProduct.quality.in_(quality_vals))

    # Size filter (OR'd when multiple, exact match against a distinct size value)
    size_vals = []
    if size:
        for s in size:
            if s and s != "ALL":
                for part in s.split(","):
                    st = part.strip()
                    if st:
                        size_vals.append(st)
    if size_vals:
        filters.append(LCProduct.size.in_(size_vals))

    # Date range filter
    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            filters.append(LCMaster.lc_date >= from_date)
        except:
            pass

    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            filters.append(LCMaster.lc_date <= to_date)
        except:
            pass

    # Apply all filters
    if filters:
        query = query.filter(and_(*filters))

    # Get total count before pagination
    total_count = query.count()

    # Apply sorting
    sort_column_map = {
        "lc_number": LCMaster.lc_number,
        "lc_date": LCMaster.lc_date,
        "expiry_date": LCMaster.expiry_date,
        "monitoring_expiry": LCMaster.monitoring_expiry,
        "status": LCMaster.status,
        "product_code": LCProduct.product_code,
        "product_name": LCProduct.product_name,
        "origin": LCProduct.origin,
        "quality": LCProduct.quality
    }

    sort_column = sort_column_map.get(sort_by, LCMaster.lc_date)

    if sort_order.lower() == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # Apply pagination
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    # Execute query
    results = query.all()

    # BL count per LC — one query, dict lookup per row
    from sqlalchemy import func as sqlfunc
    bl_count_rows = db.query(
        BillOfLading.lc_id,
        sqlfunc.count(BillOfLading.bl_id).label('cnt')
    ).group_by(BillOfLading.lc_id).all()
    bl_counts = {r.lc_id: r.cnt for r in bl_count_rows}

    # Format results with formula determination
    lcs = []
    for row in results:
        formula_id = determine_formula(row.product_code, row.origin, row.quality)

        lcs.append({
            # Core LC
            "lc_id": row.lc_id,
            "lc_number": row.lc_number,
            "lc_date": row.lc_date.isoformat() if row.lc_date else None,
            "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None,
            "last_ship_date": row.last_ship_date.isoformat() if row.last_ship_date else None,
            "monitoring_expiry": row.monitoring_expiry.isoformat() if row.monitoring_expiry else None,
            "currency": row.currency,
            "lc_amount": float(row.lc_amount) if row.lc_amount else None,
            "status": row.status,
            "contract_number": row.contract_number,
            "supplier_name": row.supplier_name,
            "importer_name": row.importer_name,
            "hoa": row.hoa,
            "payment_terms": row.payment_terms,
            # Product
            "line_id": row.line_id,
            "product_code": row.product_code,
            "product_name": row.product_name,
            "item_review": bool(row.item_review),
            "origin": row.origin,
            "quality": row.quality,
            "formula_id": formula_id,
            "lc_unit_price": float(row.lc_unit_price) if row.lc_unit_price else None,
            "quantity": float(row.quantity) if row.quantity else None,
            "unit": row.unit,
            "hs_code": row.hs_code,
            "grade": row.grade,
            "size": row.size,
            "cargo_nature": row.cargo_nature,
            # LME
            "baseline_lme": float(row.baseline_lme) if row.baseline_lme else None,
            "current_lme": float(row.current_lme) if row.current_lme else None,
            "lme_change_amount": float(row.lme_change_amount) if row.lme_change_amount else None,
            "lme_change_percent": float(row.lme_change_percent) if row.lme_change_percent else None,
            "lme_date_from": row.lme_date_from.isoformat() if row.lme_date_from else None,
            "lme_date_to": row.lme_date_to.isoformat() if row.lme_date_to else None,
            "lc_lme_difference": float(row.lc_lme_difference) if row.lc_lme_difference else None,
            "last_lme_update": row.last_lme_update.isoformat() if row.last_lme_update else None,
            # Shipment fields (populated after DB migration + import)
            "bank_name": getattr(row, 'bank_name', None),
            "booked_by": getattr(row, 'booked_by', None),
            "indentor": getattr(row, 'indentor', None),
            "insurance": getattr(row, 'insurance', None),
            "delivery_terms": getattr(row, 'delivery_terms', None),
            "exchange_rate": float(getattr(row, 'exchange_rate', None)) if getattr(row, 'exchange_rate', None) else None,
            "vessel_name": getattr(row, 'vessel_name', None),
            "bl_number": getattr(row, 'bl_number', None),
            "ship_on_board": getattr(row, 'ship_on_board', None),
            "eta": getattr(row, 'eta', None),
            "arrival_port": getattr(row, 'arrival_port', None),
            "invoice_number": getattr(row, 'invoice_number', None),
            "invoice_date": getattr(row, 'invoice_date', None),
            "shipped_quantity": float(getattr(row, 'shipped_quantity', None)) if getattr(row, 'shipped_quantity', None) else None,
            "pkgs_coils": getattr(row, 'pkgs_coils', None),
            "num_containers": getattr(row, 'num_containers', None),
            "bl_count": bl_counts.get(row.lc_id, 0),
        })

    # Calculate pagination info
    total_pages = (total_count + page_size - 1) // page_size

    return {
        "success": True,
        "data": lcs,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    }


@router.get("/filter-options")
def get_filter_options(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """
    Get all available filter options
    """

    # Get unique statuses
    statuses = db.query(LCMaster.status).distinct().all()
    status_list = [s[0] for s in statuses if s[0]]

    # Get unique product codes
    products = db.query(LCProduct.product_code).distinct().all()
    product_list = sorted([p[0] for p in products if p[0]])

    # Get unique origins
    origins = db.query(LCProduct.origin).distinct().all()
    origin_list = sorted([o[0] for o in origins if o[0]])

    # Get unique qualities
    qualities = db.query(LCProduct.quality).distinct().all()
    quality_list = sorted([q[0] for q in qualities if q[0]])

    # Get unique sizes (for size-wise filter)
    sizes = db.query(LCProduct.size).distinct().all()
    size_list = sorted([s[0] for s in sizes if s[0] and str(s[0]).strip()])

    return {
        "success": True,
        "filters": {
            "statuses": status_list,
            "products": product_list,
            "origins": origin_list,
            "qualities": quality_list,
            "sizes": size_list
        }
    }


def _pdate(v):
    """Parse an ISO date string / datetime into a date; '' and None -> None."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _pdec(v):
    """Parse a numeric into Decimal; '' and None -> None."""
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


# Fields that map straight through to LCMaster (string columns)
_LC_STR_FIELDS = [
    "lc_number", "contract_number", "supplier_name", "importer_name", "currency",
    "hoa", "payment_terms", "delivery_terms", "bank_name", "booked_by", "indentor",
    "insurance", "reimbursement", "arrival_port", "status",
]
_LC_DATE_FIELDS = ["contract_date", "lc_date", "expiry_date", "last_ship_date"]
# Product line fields
_PROD_STR_FIELDS = ["product_code", "product_name", "origin", "quality", "hs_code", "grade", "size"]
_PROD_DEC_FIELDS = ["quantity", "lc_unit_price", "lc_amount"]


@router.get("/{lc_id}/completeness")
def lc_completeness(
    lc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db),
):
    """Progress % for LC journey across all shipments."""
    return journey_svc.lc_completeness(lc_id, db)


@router.get("/{lc_id}")
def get_lc(
    lc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db),
):
    """Full LCMaster + first product line, flattened — used to pre-fill the edit form."""
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise HTTPException(status_code=404, detail="LC not found")
    prod = db.query(LCProduct).filter(LCProduct.lc_id == lc_id).order_by(LCProduct.line_id).first()

    def d(v):
        return v.isoformat() if isinstance(v, (date, datetime)) else v

    def n(v):
        return float(v) if v is not None else None

    out = {
        "lc_id": lc.lc_id, "source": lc.source, "contract_id": lc.contract_id,
        "lc_number": lc.lc_number, "contract_number": lc.contract_number,
        "contract_date": d(lc.contract_date), "supplier_name": lc.supplier_name,
        "importer_name": lc.importer_name, "lc_date": d(lc.lc_date),
        "expiry_date": d(lc.expiry_date), "last_ship_date": d(lc.last_ship_date),
        "currency": lc.currency, "exchange_rate": n(lc.exchange_rate), "hoa": lc.hoa,
        "payment_terms": lc.payment_terms, "delivery_terms": lc.delivery_terms,
        "bank_name": lc.bank_name, "booked_by": lc.booked_by, "indentor": lc.indentor,
        "insurance": lc.insurance, "reimbursement": lc.reimbursement,
        "arrival_port": lc.arrival_port, "status": lc.status,
        # product line
        "product_code": prod.product_code if prod else None,
        "product_name": prod.product_name if prod else None,
        "origin": prod.origin if prod else None,
        "quality": prod.quality if prod else None,
        "hs_code": prod.hs_code if prod else None,
        "grade": prod.grade if prod else None,
        "size": prod.size if prod else None,
        "quantity": n(prod.quantity) if prod else None,
        "unit": (prod.unit if prod else None) or "MT",
        "lc_unit_price": n(prod.lc_unit_price) if prod else None,
        "lc_amount": n(prod.lc_amount) if prod else None,
        # structured buyer / booked-by allocation (legacy booked_by text above is separate)
        "buyer_allocation": serialize_allocations(lc),
    }
    return {"success": True, "data": out}


@router.put("/{lc_id}")
def update_lc(
    lc_id: int,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db),
):
    """Update LCMaster header fields + the (first) product line. Works for any LC source."""
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise HTTPException(status_code=404, detail="LC not found")

    if "lc_number" in data and not str(data.get("lc_number") or "").strip():
        raise HTTPException(status_code=400, detail="LC Number cannot be empty")
    if "status" in data and data.get("status") and data["status"] not in (
        "OPEN", "CLOSED", "SHIPPED", "EXPIRED", "CANCELLED"):
        raise HTTPException(status_code=400, detail="Invalid status")

    # Header (only touch keys that were actually sent, so blanks clear and omitted stay)
    for f in _LC_STR_FIELDS:
        if f in data:
            v = data[f]
            setattr(lc, f, v.strip() if isinstance(v, str) and v.strip() else (None if v == "" else v))
    for f in _LC_DATE_FIELDS:
        if f in data:
            setattr(lc, f, _pdate(data[f]))
    if "exchange_rate" in data:
        lc.exchange_rate = _pdec(data["exchange_rate"])
    # keep monitoring_expiry sensible (it's NOT NULL): expiry, else lc_date, else today
    lc.monitoring_expiry = lc.expiry_date or lc.lc_date or date.today()

    # Product line — update the first, or create one if this LC somehow has none
    prod = db.query(LCProduct).filter(LCProduct.lc_id == lc_id).order_by(LCProduct.line_id).first()
    prod_keys = _PROD_STR_FIELDS + _PROD_DEC_FIELDS + ["unit"]
    if any(k in data for k in prod_keys):
        if not prod:
            prod = LCProduct(lc_id=lc_id, product_code="LC", origin="UNKNOWN",
                             quality="PRIME", quantity=Decimal("0"), lc_unit_price=Decimal("0"))
            db.add(prod)
        for f in _PROD_STR_FIELDS:
            if f in data:
                v = data[f]
                setattr(prod, f, v.strip() if isinstance(v, str) and v.strip() else (None if v == "" else v))
        if "unit" in data:
            prod.unit = (data["unit"] or "MT")
        for f in _PROD_DEC_FIELDS:
            if f in data:
                val = _pdec(data[f])
                # NOT NULL columns fall back to 0
                if val is None and f in ("quantity", "lc_unit_price"):
                    val = Decimal("0")
                setattr(prod, f, val)
        # required string columns must not become null
        if not prod.product_code:
            prod.product_code = "LC"
        if not prod.origin:
            prod.origin = "UNKNOWN"
        if not prod.quality:
            prod.quality = detect_quality(prod.product_name)

    # Structured buyer / booked-by allocation (only when the key is sent). Uses the current
    # LC quantity + amount to compute each buyer's share. Legacy booked_by text is untouched.
    if "buyer_allocation" in data:
        prod = prod or db.query(LCProduct).filter(
            LCProduct.lc_id == lc_id).order_by(LCProduct.line_id).first()
        lc_qty = (prod.quantity if prod else None)
        lc_amount = None
        if prod:
            lc_amount = prod.lc_amount
            if lc_amount is None and prod.quantity is not None and prod.lc_unit_price is not None:
                lc_amount = prod.quantity * prod.lc_unit_price
        db.flush()  # ensure lc.lc_id available for allocation rows
        apply_allocations(db, lc, data["buyer_allocation"], lc_qty, lc_amount)

    normalize_lc_master(lc, db)

    db.commit()
    db.refresh(lc)
    return {"success": True, "message": f"LC {lc.lc_number} updated", "lc_id": lc.lc_id}


@router.delete("/{lc_id}")
def delete_lc(
    lc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """
    Delete LC
    """

    # Check permission
    if not AuthService.check_permission(current_user, "delete_lc"):
        raise HTTPException(
            status_code=403,
            detail="Insufficient permissions to delete LC"
        )

    # Find LC
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()

    if not lc:
        raise HTTPException(status_code=404, detail="LC not found")

    # Delete products first
    db.query(LCProduct).filter(LCProduct.lc_id == lc_id).delete()

    # Delete LC
    db.delete(lc)
    db.commit()

    return {
        "success": True,
        "message": f"LC {lc.lc_number} deleted successfully"
    }
