"""
Reporting module — Vessel-Wise Report (V1).

Shipment-driven: a "vessel" = the set of shipments whose normalised vessel name matches.
Live queries (no cache table). Document presence + readiness computed here so the
frontend just renders. Legacy LCs (vessel on lc_master but no shipment) are surfaced
separately. See docs/VESSEL_REPORT_PLAN.md.
"""

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.weboc.helpers.gd_balance_report import gd_balance_report, gd_balance_filter_options
from modules.reports import service as svc
from modules.reports.schemas import VesselBulkUpdate

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/reports", tags=["Reports"])


@router.get("/vessels")
def list_vessels(upcoming_only: bool = Query(False),
                 db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    """Picker source: distinct (normalised) vessel names that have shipments,
    with shipment count and nearest ETA. Ordered by soonest ETA first."""
    return svc.list_vessels(db, upcoming_only)


@router.post("/vessel/bulk-update")
def vessel_bulk_update(data: VesselBulkUpdate, db: Session = Depends(get_tenant_db),
                       current_user: User = Depends(get_current_user)):
    """Bulk-update ETA and/or Port Status for EVERY shipment of one normalised vessel.
    Body: {vessel, eta?, port_status?}. Variations (EFFIE, EFFIE V, EFFIE V.) collapse to
    one vessel via the normalised key, so all their shipments update together."""
    try:
        return svc.bulk_update_vessel(db, data, current_user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/vessel")
def vessel_report(vessel: str = Query(..., description="Vessel name (normalised or raw)"),
                  eta_from: str = Query(None), eta_to: str = Query(None),
                  port: str = Query(None),
                  db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(get_current_user)):
    """Full vessel summary: KPIs, readiness, LC-wise rows, item-type / booked-by breakups,
    amount summary, missing-documents, and legacy (no-shipment) LCs."""
    return svc.vessel_report(db, vessel, eta_from, eta_to, port)


@router.get("/banks")
def bank_report(date_from: str = Query(None), date_to: str = Query(None),
                bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                lc_number: str = Query(None),
                company: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                item: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                booked_by: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                payment_term: str = Query(None),
                indentor: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                db: Session = Depends(get_tenant_db),
                current_user: User = Depends(get_current_user)):
    """Bank-Wise Report + Custom LC Report. LCs grouped by normalised issuing bank (with
    per-bank LC list, quantity & LC-amount totals + grand total) plus a flat LC-level list
    for the Custom LC Report. Filters: lc_date range, bank, LC number, company, item, booked_by, payment_term, indentor."""
    return svc.bank_report(db, date_from, date_to, bank, lc_number, company, item, booked_by, payment_term, indentor)


@router.get("/buyers")
def buyer_report(date_from: str = Query(None), date_to: str = Query(None),
                 buyer: str = Query(None),
                 db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    """Buyer / Booked-By wise report. Splits each LC across its buyers using the structured
    allocation (lc_buyer_allocations); LCs without a structured allocation fall back to the
    legacy booked_by text so nothing is lost. Returns buyer-wise qty/amount/LC-count plus
    breakdowns by item, vessel, bank and company. Filters: lc_date range + optional buyer."""
    return svc.buyer_report(db, date_from, date_to, buyer)


# ---------------------------------------------------------------------------
# GD Balance Detail report — Into-Bond GDs: what is still in bond, when its
# 180-day window expires, and the duty position (paid vs still owed).
# ---------------------------------------------------------------------------
def _rep_date(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@router.get("/gd-balance")
def gd_balance(
    date_from: str = Query(None, description="IB GD date >= (YYYY-MM-DD)"),
    date_to: str = Query(None, description="IB GD date <= (YYYY-MM-DD)"),
    vessel: Optional[List[str]] = Query(None, description="Vessel name (partial match) — OR'd when multiple"),
    shipment_id: Optional[List[str]] = Query(None, description="Shipment id(s) — OR'd when multiple"),
    gd_id: int = Query(None),
    company: Optional[List[str]] = Query(None, description="Company short code (exact) — OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="Issuing bank (exact, canonical name) — OR'd when multiple"),
    lc_number: str = Query(None, description="LC number (partial match)"),
    gd_status: Optional[List[str]] = Query(None, description="GD workflow status (exact) — OR'd when multiple"),
    declaration_type: Optional[List[str]] = Query(None, description="Declaration type as printed on GD View (partial) — OR'd when multiple"),
    sro_applicable: Optional[List[str]] = Query(None, description="SRO quota match: yes or no — OR'd when multiple"),
    late_penalty: Optional[List[str]] = Query(None, description="Late filing / Section 82 penalty: yes or no — OR'd when multiple"),
    delivery_status: Optional[List[str]] = Query(None, description="Shipment delivery: DELIVERED or PENDING — OR'd when multiple"),
    include_settled: bool = Query(True, description="Include GDs already settled"),
    gd_type: str = Query("ALL", description="GD type: ALL, HC (Home Consumption), IB (Into-Bond), EX (Ex-Bond)"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """GD balance + duty position for the selected GD type. Default ALL = every GD type."""
    # UI sends the short code; the model stores the full enum.
    GD_TYPE_MAP = {"HC": "HOME_CONSUMPTION", "IB": "INTO_BOND", "EX": "EX_BOND", "ALL": "ALL"}
    gd_type_enum = GD_TYPE_MAP.get((gd_type or "ALL").upper(), "ALL")
    out = gd_balance_report(
        db,
        date_from=_rep_date(date_from),
        date_to=_rep_date(date_to),
        vessel=vessel,
        shipment_id=shipment_id,
        gd_id=gd_id,
        company=company,
        bank=bank,
        lc_number=lc_number,
        gd_status=gd_status,
        declaration_type=declaration_type,
        sro_applicable=sro_applicable,
        late_penalty=late_penalty,
        delivery_status=delivery_status,
        include_settled=include_settled,
        gd_type=gd_type_enum,
    )
    out["filters"] = {
        "date_from": date_from, "date_to": date_to, "vessel": vessel,
        "shipment_id": shipment_id, "gd_id": gd_id, "include_settled": include_settled,
        "gd_type": (gd_type or "ALL").upper(),
        "company": company, "bank": bank, "lc_number": lc_number,
        "gd_status": gd_status, "declaration_type": declaration_type,
        "sro_applicable": sro_applicable, "late_penalty": late_penalty,
        "delivery_status": delivery_status,
    }
    out["options"] = gd_balance_filter_options(db, gd_type_enum)
    return out


# ---------------------------------------------------------------------------
# Shipment-Wise Report — one row per shipment across its full lifecycle.
# ---------------------------------------------------------------------------
@router.get("/shipment-wise")
def shipment_wise(
    date_field: str = Query("eta"),
    date_from: str = Query(None),
    date_to: str = Query(None),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    hoa: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    status: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    lc_id: Optional[int] = Query(None, description="Scope the whole report to one LC"),
    q: str = Query(None),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return svc.shipment_wise_report(
        db, date_field=date_field, date_from=date_from, date_to=date_to,
        buyer=buyer, hoa=hoa, vessel=vessel, status=status, lc_id=lc_id, q=q,
    )


# ---------------------------------------------------------------------------
# Main Report — the full shipment ledger, printable/exportable.
# ---------------------------------------------------------------------------
@router.get("/main-report")
def main_report(
    search: str = Query(None, description="LC number / vessel / party / lot / ref"),
    status: Optional[List[str]] = Query(None, description="Shipment status (exact) — OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="Vessel name (partial match) — OR'd when multiple"),
    eta_from: str = Query(None, description="ETA >= (YYYY-MM-DD)"),
    eta_to: str = Query(None, description="ETA <= (YYYY-MM-DD)"),
    importer: Optional[List[str]] = Query(None, description="Importer / company (short code) — OR'd when multiple"),
    item_type: Optional[List[str]] = Query(None, description="Item type / category (exact) — OR'd when multiple"),
    party: Optional[List[str]] = Query(None, description="Party name — booked-by, else importer — OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="Issuing bank (exact) — OR'd when multiple"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    out = svc.main_report(
        db, search=search, status=status, vessel=vessel,
        eta_from=_rep_date(eta_from), eta_to=_rep_date(eta_to),
        importer=importer, item_type=item_type, party=party, bank=bank,
    )
    out["filters"] = {
        "search": search, "status": status, "vessel": vessel,
        "eta_from": eta_from, "eta_to": eta_to,
        "importer": importer, "item_type": item_type, "party": party, "bank": bank,
    }
    return out


# ---------------------------------------------------------------------------
# Pending Order Report — open/in-pipeline LCs with item grouping.
# ---------------------------------------------------------------------------
@router.get("/pending-orders")
def pending_orders_report(
    buyer: Optional[List[str]] = Query(None, description="Buyer / booked-by (partial match) — OR'd when multiple"),
    item: Optional[List[str]] = Query(None, description="Item type (exact) — OR'd when multiple"),
    origin: Optional[List[str]] = Query(None, description="Origin (exact) — OR'd when multiple"),
    importer: Optional[List[str]] = Query(None, description="Importer / company (exact) — OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="Issuing bank (exact, normalised) — OR'd when multiple"),
    search: str = Query(None, description="LC number / vessel / buyer / origin"),
    lc_date_from: str = Query(None, description="LC date >= (YYYY-MM-DD)"),
    lc_date_to: str = Query(None, description="LC date <= (YYYY-MM-DD)"),
    eta_from: str = Query(None, description="ETA >= (YYYY-MM-DD)"),
    eta_to: str = Query(None, description="ETA <= (YYYY-MM-DD)"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Pending Order Report — open / in-pipeline LCs with item grouping (matches the manual
    upcoming-orders sheet: LC #, item, size, buyer, origin, qty, rate, dates, ETA)."""
    data = svc.pending_order_report(
        db,
        buyer=buyer,
        item=item,
        origin=origin,
        importer=importer,
        bank=bank,
        search=search,
        lc_date_from=_rep_date(lc_date_from),
        lc_date_to=_rep_date(lc_date_to),
        eta_from=_rep_date(eta_from),
        eta_to=_rep_date(eta_to),
    )
    data["filters"] = {
        "buyer": buyer, "item": item, "origin": origin, "importer": importer,
        "bank": bank, "search": search,
        "lc_date_from": lc_date_from, "lc_date_to": lc_date_to,
        "eta_from": eta_from, "eta_to": eta_to,
    }
    return data
