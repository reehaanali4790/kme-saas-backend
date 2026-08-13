"""
Shipment API — the central hub grouping BL + Invoice + Packing + GD under an LC.
CRUD + LC-anchored creation + running balance + document aggregation.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from core.tenant import get_tenant_db
from models.database_models import (
    ActivityLog, CommercialInvoice, GoodsDeclaration, LCMaster, PackingList, Shipment, User,
)
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from modules.lc_creation.helpers.shipment_validator import validate_shipment
from modules.shipments import services as svc
from modules.shipments import journey_service as journey_svc
from modules.shipments.schemas import ShipmentCreate, ShipmentCreateResult, ShipmentUpdate
from modules.workflow.helpers import check_gate, check_shipment_create
from modules.workflow.constants import ACTION_SET_DELIVERY

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/shipments", tags=["Shipments"])

# Mutations require OPERATOR+ - VIEWER can read shipments but not create/edit/delete them.
_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


# ---------------------------------------------------------------------------
# Create — LC-anchored
# ---------------------------------------------------------------------------

@router.post("/", response_model=ShipmentCreateResult)
def create_shipment(data: ShipmentCreate, request: Request, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    check_shipment_create(db, request, lc_id=data.lc_id, user_id=current_user.user_id,
                          override_reason=data.override_reason)
    shipment, balance = svc.create_shipment(data, db, created_by=current_user.user_id)
    logger.info(f"Shipment created: id={shipment.shipment_id}, lc_id={data.lc_id}, "
                f"category={shipment.category}")
    return ShipmentCreateResult(shipment_id=shipment.shipment_id, category=shipment.category,
                                 lc_balance=balance)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("/")
def list_shipments(
    status: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    lc_id: Optional[int] = Query(None),
    validation_status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    # selectinload (not joinedload) for every parallel to-many collection here: joining
    # them all in one query produces a cross-product of rows per shipment that SQLAlchemy
    # then has to de-dupe client-side, and inflates the q.count() below. selectinload issues
    # one extra IN-query per relationship instead, which stays flat regardless of fan-out.
    q = db.query(Shipment).filter(Shipment.is_deleted.is_(False)).options(
        joinedload(Shipment.lc).selectinload(LCMaster.products),
        selectinload(Shipment.bill_of_ladings),
        selectinload(Shipment.commercial_invoices),
        selectinload(Shipment.packing_lists),
        selectinload(Shipment.goods_declarations),
        selectinload(Shipment.financial_instruments),
        selectinload(Shipment.extra_documents),
    )
    if status:
        status_vals = []
        for s in status:
            if s:
                for part in str(s).split(","):
                    p = part.strip()
                    if p:
                        status_vals.append(p.upper())
        if status_vals:
            q = q.filter(Shipment.status.in_(status_vals))
    if validation_status:
        q = q.filter(Shipment.validation_status == validation_status.upper())
    if lc_id:
        q = q.filter(Shipment.lc_id == lc_id)
    if search:
        term = f"%{search.upper()}%"
        q = q.filter(
            Shipment.shipment_ref.ilike(term) |
            Shipment.vessel_name.ilike(term) |
            Shipment.port_of_discharge.ilike(term) |
            Shipment.lot_number.ilike(term) |
            # search by LC number (related LCMaster)
            Shipment.lc.has(LCMaster.lc_number.ilike(term)) |
            # search by commercial invoice number
            Shipment.commercial_invoices.any(CommercialInvoice.invoice_number.ilike(term))
        )

    total = q.count()
    rows = q.order_by(Shipment.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [svc.shipment_summary(s) for s in rows]}


# ---------------------------------------------------------------------------
# Stats (dashboard header)
# ---------------------------------------------------------------------------

@router.get("/stats")
def shipment_stats(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    base = db.query(func.count(Shipment.shipment_id)).filter(Shipment.is_deleted.is_(False))
    total = base.scalar() or 0
    discrepant = base.filter(Shipment.validation_status == "DISCREPANT").scalar() or 0
    pending = db.query(func.count(Shipment.shipment_id)).filter(
        Shipment.is_deleted.is_(False), Shipment.status == "PENDING").scalar() or 0
    # shipments missing a GD
    with_gd = db.query(GoodsDeclaration.shipment_id).distinct().subquery()
    gd_pending = db.query(func.count(Shipment.shipment_id)).filter(
        Shipment.is_deleted.is_(False),
        ~Shipment.shipment_id.in_(db.query(with_gd.c.shipment_id))).scalar() or 0
    return {"total": total, "pending_docs": pending,
            "discrepancies": discrepant, "gd_pending": gd_pending}


# ---------------------------------------------------------------------------
# By LC (single-LC detail view data)
# ---------------------------------------------------------------------------

@router.get("/by-lc/{lc_id}")
def shipments_for_lc(lc_id: int, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(get_current_user)):
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise HTTPException(status_code=404, detail="LC not found")
    rows = db.query(Shipment).options(
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.commercial_invoices),
        joinedload(Shipment.packing_lists),
        joinedload(Shipment.goods_declarations),
    ).filter(Shipment.lc_id == lc_id, Shipment.is_deleted.is_(False)) \
     .order_by(Shipment.created_at.asc()).all()
    return {
        "lc_id": lc_id, "lc_number": lc.lc_number,
        "balance": svc.compute_lc_balance(lc_id, db),
        "shipments": [svc.shipment_summary(s) for s in rows],
    }


@router.get("/lc-balance/{lc_id}")
def lc_balance(lc_id: int, db: Session = Depends(get_tenant_db),
               current_user: User = Depends(get_current_user)):
    return svc.compute_lc_balance(lc_id, db)


# ---------------------------------------------------------------------------
# Validate (cross-document checks)
# ---------------------------------------------------------------------------

@router.get("/{shipment_id}/journey")
def shipment_journey(shipment_id: int, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(get_current_user)):
    """Guided workflow steps with prerequisites, blockers, and deep links."""
    return journey_svc.build_journey(shipment_id, db)


@router.get("/{shipment_id}/doc-status")
def shipment_doc_status(shipment_id: int, db: Session = Depends(get_tenant_db),
                        current_user: User = Depends(get_current_user)):
    """Per-document upload status, expected-by dates, and WhatsApp-critical flags."""
    return journey_svc.build_doc_status(shipment_id, db)


@router.get("/{shipment_id}/timeline")
def shipment_timeline(shipment_id: int, db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(get_current_user)):
    """All critical milestone dates for one shipment."""
    return journey_svc.build_timeline(shipment_id, db)


@router.post("/{shipment_id}/validate")
def run_validation(shipment_id: int, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(get_current_user)):
    svc.get_shipment_or_404(shipment_id, db)
    return validate_shipment(shipment_id, db)


# ---------------------------------------------------------------------------
# Get single (full detail)
# ---------------------------------------------------------------------------

@router.get("/{shipment_id}")
def get_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    s = svc.get_shipment_or_404(shipment_id, db, options=[
        joinedload(Shipment.lc),
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.commercial_invoices).joinedload(CommercialInvoice.line_items),
        joinedload(Shipment.packing_lists).joinedload(PackingList.line_items),
        joinedload(Shipment.goods_declarations),
        joinedload(Shipment.financial_instruments),
        joinedload(Shipment.insurance_certificates),
        joinedload(Shipment.validations),
    ])
    return svc.shipment_detail(s, db)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

@router.put("/{shipment_id}")
def update_shipment(shipment_id: int, data: ShipmentUpdate, request: Request,
                    db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    if data.delivery_date is not None:
        check_gate(db, request, shipment_id, ACTION_SET_DELIVERY,
                   user_id=current_user.user_id, override_reason=data.override_reason)
    svc.update_shipment(shipment_id, data, db, current_user.user_id)
    return {"success": True, "shipment_id": shipment_id}


@router.get("/{shipment_id}/activity")
def shipment_activity(shipment_id: int, db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(get_current_user)):
    """Activity & Document Trail for a shipment — newest first."""
    rows = (db.query(ActivityLog, User.full_name)
              .outerjoin(User, ActivityLog.user_id == User.user_id)
              .filter(ActivityLog.shipment_id == shipment_id)
              .order_by(ActivityLog.created_at.desc(), ActivityLog.log_id.desc())
              .limit(200).all())
    items = [{
        "log_id": a.log_id,
        "user_name": full_name or "System",
        "action": a.action,
        "doc_type": a.doc_type,
        "detail": a.detail,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a, full_name in rows]
    return {"shipment_id": shipment_id, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@router.delete("/{shipment_id}")
def delete_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    """Soft delete — the shipment is hidden from lists/reports/balances but all its
    documents and data are preserved on disk and can be restored."""
    deleted_now = svc.delete_shipment(shipment_id, db, current_user.user_id)
    if not deleted_now:
        return {"success": True, "shipment_id": shipment_id, "already_deleted": True}
    logger.info(f"Shipment soft-deleted: id={shipment_id}, by={current_user.username}")
    return {"success": True, "shipment_id": shipment_id}


@router.post("/{shipment_id}/restore")
def restore_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(_can_write)):
    """Undo a soft delete."""
    svc.restore_shipment(shipment_id, db, current_user.user_id)
    return {"success": True, "shipment_id": shipment_id}


# ---------------------------------------------------------------------------
# LC search (for the "select LC" step when creating a shipment)
# ---------------------------------------------------------------------------

@router.get("/search/lcs")
def search_lcs(q: str = Query(..., min_length=1), db: Session = Depends(get_tenant_db),
               current_user: User = Depends(get_current_user)):
    term = f"%{q.upper()}%"
    lcs = db.query(LCMaster).filter(
        (LCMaster.lc_number.ilike(term)) | (LCMaster.supplier_name.ilike(term))
    ).order_by(LCMaster.lc_date.desc()).limit(15).all()
    out = []
    for lc in lcs:
        bal = svc.compute_lc_balance(lc.lc_id, db)
        out.append({
            "lc_id": lc.lc_id, "lc_number": lc.lc_number,
            "supplier_name": lc.supplier_name,
            "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
            "status": lc.status, "shipment_count": len(lc.shipments),
            **bal,
        })
    return out
