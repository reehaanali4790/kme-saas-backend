"""
Automatic shipment status derivation.

Shipment status is NOT set manually — it is derived from milestone dates and the
presence of the three "copy" documents. The most advanced applicable stage wins:

    DELIVERED               <- delivery_date set
    ORIGINAL_DOCS_RECEIVED  <- original_doc_date set
    PAYMENT_RECEIVED        <- payment_date set
    DOCS_AT_BANK            <- intimation_date set
    COPY_DOCS_RECEIVED      <- Commercial Invoice + Packing List + Bill of Lading all present
    PENDING                 <- default (nothing yet)

Call recompute_shipment_status(shipment) after any change to those dates or documents.
"""

# High -> low priority (index = advancement order).
SHIPMENT_FLOW = [
    "PENDING",
    "COPY_DOCS_RECEIVED",
    "DOCS_AT_BANK",
    "PAYMENT_RECEIVED",
    "ORIGINAL_DOCS_RECEIVED",
    "DELIVERED",
]

SHIPMENT_STATUS_LABEL = {
    "PENDING": "Pending",
    "COPY_DOCS_RECEIVED": "Copy Documents Received",
    "DOCS_AT_BANK": "Documents Received at Bank",
    "PAYMENT_RECEIVED": "Payment Received",
    "ORIGINAL_DOCS_RECEIVED": "Original Documents Received",
    "DELIVERED": "Delivered",
    # legacy labels (for un-backfilled rows)
    "DOCUMENTS_RECEIVED": "Copy Documents Received",
    "VALIDATED": "Validated",
    "DISCREPANT": "Discrepant",
    "BANK_PRESENTATION": "Documents Received at Bank",
    "ACCEPTED": "Accepted",
    "PAYMENT_MADE": "Payment Received",
    "CLOSED": "Closed",
}


def _has_copy_documents(shipment) -> bool:
    """True when the three copy documents (Invoice + Packing + BL) are all present."""
    return bool(shipment.commercial_invoices) and \
        bool(shipment.packing_lists) and \
        bool(shipment.bill_of_ladings)


def compute_shipment_status(shipment) -> str:
    """Return the most-advanced applicable status for this shipment (see module docstring)."""
    if shipment.delivery_date:
        return "DELIVERED"
    if shipment.original_doc_date:
        return "ORIGINAL_DOCS_RECEIVED"
    if shipment.payment_date:
        return "PAYMENT_RECEIVED"
    if shipment.intimation_date:
        return "DOCS_AT_BANK"
    if _has_copy_documents(shipment):
        return "COPY_DOCS_RECEIVED"
    return "PENDING"


def recompute_shipment_status(shipment) -> str:
    """Derive and assign the shipment's status in place. Returns the new status."""
    shipment.status = compute_shipment_status(shipment)
    return shipment.status


# ---------------------------------------------------------------------------
# CRUD orchestration + read-view assembly
#
# Extracted from modules/shipments/router.py as part of the Phase 4 module rollout.
# shipment_summary()/shipment_detail() stay plain dict builders (not Pydantic response
# schemas) - this hub view aggregates 6+ related document types into a large, actively
# evolving structure; see schemas/bank_limits.py's /report endpoint for the same call.
# ---------------------------------------------------------------------------
import os
import re
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError
from infrastructure.activity.activity_service import log_activity
from infrastructure.normalization.normalization_service import payment_tenor
from models.database_models import LCMaster, LCProduct, Shipment
from modules.shipments.schemas import ShipmentCreate, ShipmentUpdate
from modules.shipments.shipment_metrics import resolve_coils, resolve_net_weight_mt
from modules.shipments.eta_calc import estimate_eta
from modules.shipments.vessel_status_service import apply_port_status, resolve_vessel_status, sync_bl_demurrage_from_departure
from utils.parsing import parse_float

CATEGORY_ORDINALS = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH",
                     "SEVENTH", "EIGHTH", "NINTH", "TENTH"]

# For DA (usance) LCs, documents are expected to arrive within this many days of the
# BL / shipped-on-board date. Drives the shipment-page doc-arrival alert.
DA_DOC_ARRIVAL_DAYS = 5


def ordered_docs(items):
    """Order a shipment's documents so the PRIMARY one is first: verified/saved records
    before unsaved PENDING_REVIEW placeholders, then most recently created first.
    A shipment is meant to hold one doc of each type, but abandoned uploads can leave
    stale placeholders — this makes the UI/validator pick the real, latest record."""
    if not items:
        return []
    return sorted(
        items,
        key=lambda d: (
            1 if getattr(d, "status", None) == "PENDING_REVIEW" else 0,
            -(d.created_at.timestamp() if getattr(d, "created_at", None) else 0),
        ),
    )


def _quality_from_text(*values: Optional[str]) -> Optional[str]:
    """Infer PRIME/SECONDARY from noisy free text (LC + BL + Invoice + Packing)."""
    merged = " ".join(str(v or "") for v in values).upper()
    if not merged.strip():
        return None
    # Be tolerant to common OCR/typing noise: "SECONDAY", "NPRM", "NON PRIME", etc.
    if re.search(r"\bSECONDARY\b|\bSECONDAY\b|\bNPRM\b|\bNON[\s\-]?PRIME\b|\b2ND\b|\bSEC\b", merged):
        return "SECONDARY"
    if re.search(r"\bPRIME\b|\bPRM\b", merged):
        return "PRIME"
    return None


def compute_lc_balance(lc_id: int, db: Session) -> dict:
    """LC ordered qty (sum of product lines) vs delivered (sum of shipments) -> remaining."""
    ordered = db.query(func.coalesce(func.sum(LCProduct.quantity), 0)).filter(
        LCProduct.lc_id == lc_id
    ).scalar() or Decimal(0)

    delivered = db.query(func.coalesce(func.sum(Shipment.delivered_quantity_mt), 0)).filter(
        Shipment.lc_id == lc_id,
        Shipment.is_deleted.is_(False),
    ).scalar() or Decimal(0)

    ordered = Decimal(str(ordered))
    delivered = Decimal(str(delivered))
    remaining = ordered - delivered
    return {
        "lc_ordered_mt": float(ordered),
        "lc_delivered_mt": float(delivered),
        "lc_remaining_mt": float(remaining),
    }


def next_category(lc_id: int, db: Session) -> str:
    count = db.query(func.count(Shipment.shipment_id)).filter(Shipment.lc_id == lc_id).scalar() or 0
    return CATEGORY_ORDINALS[count] if count < len(CATEGORY_ORDINALS) else f"#{count + 1}"


def _buyer_party(lc: Optional[LCMaster]) -> Optional[str]:
    """Buyer / party name for reports (JM, AM, etc.) — NOT the LC importer company."""
    if not lc:
        return None
    if lc.buyer_allocation_type and lc.buyer_allocations:
        names = []
        for a in lc.buyer_allocations:
            n = (a.buyer_name or "").strip()
            if n and n not in names:
                names.append(n)
        if names:
            return ", ".join(names) if len(names) > 1 else names[0]
    booked = (lc.booked_by or "").strip()
    return booked or None


# ~1 kg tolerance when comparing KGTL total (MT) to the GD's declared gross weight.
_KGTL_QTY_MATCH_TOL_MT = 0.001


def _kgtl_total_kg(gd) -> Optional[float]:
    """Sum of KGTL weighbridge weights (KG) recorded against the shipment GD."""
    if not gd:
        return None
    weights = [float(w.kgtl_weight_kg) for w in (gd.kgtl_weighments or [])
               if w.kgtl_weight_kg is not None]
    return sum(weights) if weights else None


def _kgtl_qty_compare(gd_gross_mt: Optional[float], kgtl_kg: Optional[float]):
    """KGTL total (KG→MT) vs the GD's own declared gross weight — the same basis the WeBOC
    KGTL Weighbridge Reconciliation card uses (kgtl_service.kgtl_summary), so this report
    column and that card never disagree. Previously this compared against the shipment's
    resolved NET weight (CI/PL/BL/DPL/LC waterfall) instead: a different quantity than gross,
    and one that can be unresolved even when the GD's own gross weight and the KGTL total are
    both known — which showed as "KGTL Total (MT)" and "KGTL Diff (MT)" going blank while the
    document weight column next to them was populated fine.

    Returns (kgtl_mt, diff_mt, status). kgtl_mt is available whenever kgtl_kg is, independent
    of whether the GD's gross weight has been captured yet — only diff/status wait for that.
    """
    if kgtl_kg is None:
        return None, None, None
    kgtl_mt = round(kgtl_kg / 1000.0, 3)
    if gd_gross_mt is None:
        return kgtl_mt, None, None
    diff_mt = round(kgtl_mt - gd_gross_mt, 3)
    if abs(diff_mt) <= _KGTL_QTY_MATCH_TOL_MT:
        status = "MATCH"
    elif diff_mt < 0:
        status = "SHORT"
    else:
        status = "EXCESS"
    return kgtl_mt, diff_mt, status


def shipment_summary(s: Shipment) -> dict:
    """Lightweight dict for list views."""
    inv = ordered_docs(s.commercial_invoices)[0] if s.commercial_invoices else None
    bl = ordered_docs(s.bill_of_ladings)[0] if s.bill_of_ladings else None
    pkg = ordered_docs(s.packing_lists)[0] if s.packing_lists else None
    gd = ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
    fi = ordered_docs(s.financial_instruments)[0] if s.financial_instruments else None
    lc_hs = s.lc.products[0].hs_code if (s.lc and s.lc.products) else None
    hs_codes = {
        "Invoice": inv.hs_code if inv else None,
        "Packing": pkg.hs_code if pkg else None,
        "GD": gd.hs_code if gd else None,
        "FI": fi.hs_code if fi else None,
        "LC": lc_hs,
    }
    hs_present = {k: v for k, v in hs_codes.items() if v}
    # LC payment tenor (SIGHT/DA) drives the Maturity-Date field + DA doc-arrival alert.
    lc_payment_type = payment_tenor(s.lc.payment_terms) if s.lc else None
    da_doc_expected = None
    if lc_payment_type == "DA" and s.bl_date:
        da_doc_expected = (s.bl_date + timedelta(days=DA_DOC_ARRIVAL_DAYS)).isoformat()

    # ---- LC / product-derived operational fields for the shipment listing ----
    lc = s.lc
    prods = lc.products if (lc and lc.products) else []
    prod0 = prods[0] if prods else None
    product_name = prod0.product_name if prod0 else None
    product_code = prod0.product_code if prod0 else None
    lc_quality = (prod0.quality or "").upper() if prod0 and prod0.quality else None
    # Cross-check quality text against BL / Invoice / Packing content to reduce bad labeling.
    invoice_quality = _quality_from_text(
        (inv.goods_description if inv else None),
        (inv.grade if inv else None),
    )
    bl_quality = _quality_from_text(bl.goods_description if bl else None)
    packing_quality = _quality_from_text(
        " ".join(filter(None, [str(getattr(li, "grade", "")).strip() for li in (pkg.line_items if pkg else [])]))
    )
    quality = (
        _quality_from_text(lc_quality, invoice_quality, bl_quality, packing_quality, product_name, product_code)
        or lc_quality
        or invoice_quality
        or bl_quality
        or packing_quality
    )
    # Item category: normalized short code, then legacy product_code / name
    item_category = None
    if prod0:
        item_category = prod0.item_code or prod0.product_code or prod0.product_name
    # LC rate (first product carrying a unit price)
    lc_rate = next((parse_float(p.lc_unit_price) for p in prods if p.lc_unit_price is not None), None)
    # LC amount: sum of product lc_amount, else qty*unit_price
    lc_amount = sum(float(p.lc_amount) for p in prods if p.lc_amount is not None)
    if not lc_amount:
        lc_amount = sum(float(p.quantity or 0) * float(p.lc_unit_price or 0) for p in prods)
    lc_amount = lc_amount or None
    # LME rate (first non-null current_lme), container + package/coil totals
    lme_rate = next((float(p.current_lme) for p in prods if p.current_lme is not None), None)
    containers = sum(int(p.num_containers or 0) for p in prods) or None
    # Packages/coils and net weight resolved via the CI -> PL -> BL -> DPL -> shipment -> LC
    # waterfall (shipment_metrics), not just the shipment's own reconciled totals.
    coils_val, _coil_src = resolve_coils(s)
    packages = coils_val if coils_val is not None else (sum(int(p.pkgs_coils or 0) for p in prods) or None)
    qty_mt_total, _qty_src = resolve_net_weight_mt(s)
    if qty_mt_total is None:
        qty_mt_total = sum(float(p.quantity) for p in prods if p.quantity is not None) or None
    qty_kgs = round(qty_mt_total * 1000) if qty_mt_total is not None else None
    gd_gross_mt = float(gd.gross_weight_mt) if gd and gd.gross_weight_mt is not None else None
    kgtl_kg = _kgtl_total_kg(gd)
    kgtl_mt, kgtl_diff_mt, kgtl_diff_status = _kgtl_qty_compare(gd_gross_mt, kgtl_kg)
    vs = resolve_vessel_status(s)
    return {
        "shipment_id": s.shipment_id,
        "lc_id": s.lc_id,
        "lc_number": s.lc.lc_number if s.lc else None,
        "shipment_ref": s.shipment_ref,
        "category": s.category,
        "lot_number": s.lot_number,
        "status": s.status,
        "status_label": SHIPMENT_STATUS_LABEL.get(s.status, s.status) if s.status is not None else None,
        "validation_status": s.validation_status,
        "remarks": s.remarks,
        "vessel_name": s.vessel_name,
        "voyage_number": s.voyage_number,
        "vessel_location": s.vessel_location,
        "vessel_status": vs.get("vessel_status"),
        "vessel_status_source": s.vessel_status_source,
        "vessel_status_updated_at": s.vessel_status_updated_at.isoformat() if s.vessel_status_updated_at else None,
        "country_port": vs.get("country_port"),
        "on_port_date": vs.get("on_port_date"),
        "departure_date": vs.get("departure_date"),
        "kpt_berth": vs.get("kpt_berth"),
        "port_of_loading": s.port_of_loading,
        "port_of_discharge": s.port_of_discharge,
        "bl_date": s.bl_date.isoformat() if s.bl_date else None,
        "etd": s.etd.isoformat() if s.etd else None,
        "eta": s.eta.isoformat() if s.eta else None,
        # Auto-ETA estimation — 'AUTO' (formula) / 'WEBSITE' (KPT tracker) / 'MANUAL' (human).
        # None behaves like 'AUTO' for older rows that predate this feature.
        "eta_source": s.eta_source,
        "transit_days": s.transit_days,
        "delivery_date": s.delivery_date.isoformat() if s.delivery_date else None,
        # manual milestone fields
        "payment_date": s.payment_date.isoformat() if s.payment_date else None,
        "original_doc_date": s.original_doc_date.isoformat() if s.original_doc_date else None,
        "retirement_date": s.retirement_date.isoformat() if s.retirement_date else None,
        "intimation_date": s.intimation_date.isoformat() if s.intimation_date else None,
        "maturity_date": s.maturity_date.isoformat() if s.maturity_date else None,
        "exchange_rate": parse_float(s.exchange_rate),
        # LC tenor + DA document-arrival expectation
        "lc_payment_type": lc_payment_type,
        "da_doc_expected": da_doc_expected,
        # LC / product-derived operational fields (shipment listing columns)
        "bank_name": lc.bank_name if lc else None,
        "payment_terms": lc.payment_terms if lc else None,
        "party_name": _buyer_party(lc),
        "hoa": lc.hoa if lc else None,
        "importer_name": lc.importer_name if lc else None,
        "booked_by": lc.booked_by if lc else None,
        "currency": lc.currency if lc else None,
        "item_category": item_category,
        "product_name": product_name,
        "product_code": product_code,
        "product_item_review": bool(prod0.item_review) if prod0 else False,
        "quality": quality,
        "lc_rate": lc_rate,
        "lc_amount": lc_amount,
        "lme_rate": lme_rate,
        "containers": containers,
        "packages": packages,
        "qty_mt": round(qty_mt_total, 3) if qty_mt_total is not None else None,
        "qty_kgs": qty_kgs,
        "kgtl_total_kg": round(kgtl_kg, 3) if kgtl_kg is not None else None,
        "kgtl_total_mt": kgtl_mt,
        "kgtl_diff_mt": kgtl_diff_mt,
        "kgtl_diff_status": kgtl_diff_status,
        "total_coils": s.total_coils,
        "total_net_weight_mt": parse_float(s.total_net_weight_mt),
        "total_gross_weight_mt": parse_float(s.total_gross_weight_mt),
        "expected_quantity_mt": parse_float(s.expected_quantity_mt),
        "delivered_quantity_mt": parse_float(s.delivered_quantity_mt),
        "quantity_variance_mt": parse_float(s.quantity_variance_mt),
        # document presence flags for the dashboard chips
        "has_bl": bl is not None,
        "has_invoice": inv is not None,
        "has_packing": pkg is not None,
        "has_gd": gd is not None,
        "has_fi": fi is not None,
        "bl_number": bl.bl_number if bl else None,
        "invoice_number": inv.invoice_number if inv else None,
        # HS code — primary (first available) + per-source map for discrepancy display
        "hs_code": next(iter(hs_present.values()), None),
        "hs_codes": hs_present,
    }


def shipment_detail(s: Shipment, db: Session) -> dict:
    """Full dict with all attached documents + line items + validations + LC balance."""
    from modules.documents.insurance_service import verify_one as verify_ref

    d = shipment_summary(s)
    n = parse_float

    d["bill_of_ladings"] = [{
        "bl_id": b.bl_id, "bl_number": b.bl_number,
        "bl_date": b.bl_date.isoformat() if b.bl_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": b.upload_date.isoformat() if b.upload_date else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "vessel_name": b.vessel_name, "status": b.status,
        "gross_weight_mt": n(b.gross_weight_mt),
        "package_count": b.package_count, "package_type": b.package_type,
        "has_document": bool(b.document_path and os.path.exists(b.document_path)),
    } for b in ordered_docs(s.bill_of_ladings)]

    d["commercial_invoices"] = [{
        "invoice_id": i.invoice_id, "invoice_number": i.invoice_number,
        "invoice_date": i.invoice_date.isoformat() if i.invoice_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": i.upload_date.isoformat() if i.upload_date else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "documentary_credit_number": i.documentary_credit_number,
        "unit_price_usd": n(i.unit_price_usd), "total_amount_usd": n(i.total_amount_usd),
        "total_coils": i.total_coils,
        "total_net_weight_mt": n(i.total_net_weight_mt), "total_gross_weight_mt": n(i.total_gross_weight_mt),
        "status": i.status,
        "has_document": bool(i.document_path and os.path.exists(i.document_path)),
        "line_items": [{
            "item_number": li.item_number, "size_thickness_mm": n(li.size_thickness_mm),
            "size_width_mm": n(li.size_width_mm), "quantity_mt": n(li.quantity_mt),
            "net_weight_mt": n(li.net_weight_mt), "gross_weight_mt": n(li.gross_weight_mt),
            "number_of_coils": li.number_of_coils, "unit_price_usd": n(li.unit_price_usd),
            "line_amount_usd": n(li.line_amount_usd),
        } for li in sorted(i.line_items, key=lambda x: x.item_number or 0)],
    } for i in ordered_docs(s.commercial_invoices)]

    d["packing_lists"] = [{
        "packing_id": p.packing_id, "packing_number": p.packing_number,
        "packing_date": p.packing_date.isoformat() if p.packing_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": p.upload_date.isoformat() if p.upload_date else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "total_coils": p.total_coils, "total_net_weight_mt": n(p.total_net_weight_mt),
        "total_gross_weight_mt": n(p.total_gross_weight_mt), "status": p.status,
        "has_document": bool(p.document_path and os.path.exists(p.document_path)),
        "line_items": [{
            "item_number": li.item_number, "grade": li.grade, "size": li.size,
            "size_thickness_mm": n(li.size_thickness_mm),
            "size_width_mm": n(li.size_width_mm), "quantity_mt": n(li.quantity_mt),
            "net_weight_mt": n(li.net_weight_mt), "gross_weight_mt": n(li.gross_weight_mt),
            "number_of_coils": li.number_of_coils,
        } for li in sorted(p.line_items, key=lambda x: x.item_number or 0)],
    } for p in ordered_docs(s.packing_lists)]

    d["goods_declarations"] = [{
        "gd_id": g.gd_id, "gd_number": g.gd_number,
        "filing_date": g.filing_date.isoformat() if g.filing_date else None,
        "assessed_value_pkr": n(g.assessed_value_pkr),
        "total_duties_pkr": n(g.total_duties_pkr), "status": g.status,
        "has_document": bool(g.document_path and os.path.exists(g.document_path)),
    } for g in ordered_docs(s.goods_declarations)]

    d["financial_instruments"] = [{
        "fi_id": f.fi_id, "fi_number": f.fi_number,
        "expiry_date": f.expiry_date.isoformat() if f.expiry_date else None,
        "hs_code": f.hs_code, "fi_value": n(f.fi_value), "fi_currency": f.fi_currency,
        "lc_contract_no": f.lc_contract_no, "status": f.status,
        "has_document": bool(f.document_path and os.path.exists(f.document_path)),
    } for f in ordered_docs(s.financial_instruments)]

    # Convenience fields for shipment header / alerts
    fi0 = ordered_docs(s.financial_instruments)[0] if s.financial_instruments else None
    d["fi_number"] = fi0.fi_number if fi0 else None
    d["fi_expiry_date"] = fi0.expiry_date.isoformat() if fi0 and fi0.expiry_date else None
    if fi0 and fi0.expiry_date:
        d["fi_expiry_days"] = (fi0.expiry_date - date.today()).days
    else:
        d["fi_expiry_days"] = None

    # Insurance certificate (marine insurance) — with BL/LC verification vs this shipment
    ship_bl = ordered_docs(s.bill_of_ladings)[0].bl_number if s.bill_of_ladings else None
    ship_lc = s.lc.lc_number if s.lc else None
    d["insurance_certificates"] = [{
        "id": ic.insurance_id, "insurance_id": ic.insurance_id, "certificate_number": ic.certificate_number,
        "policy_number": ic.policy_number, "bl_number": ic.bl_number, "lc_number": ic.lc_number,
        "vessel_name": ic.vessel_name, "net_premium": n(ic.net_premium),
        "insurance_company": ic.insurance_company,
        "issue_date": ic.issue_date.isoformat() if ic.issue_date else None,
        "sum_insured": n(ic.sum_insured), "currency": ic.currency,
        "voyage_route": ic.voyage_route, "assured_name": ic.assured_name,
        "status": ic.status, "document_filename": ic.document_filename,
        "has_document": bool(ic.document_path and os.path.exists(ic.document_path)),
        "verification": {
            "bl": verify_ref(ic.bl_number, ship_bl),
            "lc": verify_ref(ic.lc_number, ship_lc),
            "shipment_bl_number": ship_bl, "shipment_lc_number": ship_lc,
        },
    } for ic in ordered_docs(s.insurance_certificates)]
    d["insurance_documents"] = d["insurance_certificates"]

    d["validations"] = [{
        "check_name": v.check_name, "check_type": v.check_type, "status": v.status,
        "message": v.message,
        "bl_value": v.bl_value, "invoice_value": v.invoice_value,
        "packing_value": v.packing_value, "lc_value": v.lc_value,
    } for v in s.validations]

    if s.lc_id:
        d["lc_balance"] = compute_lc_balance(s.lc_id, db)
    return d


def get_shipment_or_404(shipment_id: int, db: Session, *, options=None) -> Shipment:
    q = db.query(Shipment)
    if options:
        q = q.options(*options)
    s = q.filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        raise NotFoundError("Shipment not found")
    return s


def create_shipment(data: ShipmentCreate, db: Session, created_by: int) -> tuple:
    lc = db.query(LCMaster).filter(LCMaster.lc_id == data.lc_id).first()
    if not lc:
        raise NotFoundError("LC not found")

    balance = compute_lc_balance(data.lc_id, db)
    shipment = Shipment(
        lc_id=data.lc_id,
        category=data.category or next_category(data.lc_id, db),
        lot_number=data.lot_number,
        shipment_ref=data.shipment_ref,
        status="PENDING",
        validation_status="PENDING",
        # suggest remaining balance as the expected quantity for variance checking
        expected_quantity_mt=Decimal(str(balance["lc_remaining_mt"])) if balance["lc_remaining_mt"] > 0 else None,
        created_by=created_by,
    )
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    return shipment, balance


def apply_shipment_fields(s: Shipment, data: ShipmentUpdate) -> None:
    fields_set = data.model_fields_set
    for field in ("shipment_ref", "category", "lot_number", "vessel_name", "voyage_number",
                  "port_of_loading", "port_of_discharge", "vessel_location", "validation_notes",
                  "remarks", "kpt_berth"):
        if field in fields_set:
            setattr(s, field, getattr(data, field))
    # A manual vessel-location edit is a human overriding whatever the KPT scraper last
    # wrote — mark the source so the UI badge flips from "Website" to "Manual" immediately,
    # even though kpt_*_at (set-once) can't tell us that on its own.
    if "vessel_location" in fields_set:
        s.vessel_status_source = "MANUAL"
        s.vessel_status_updated_at = datetime.utcnow()
    for field in ("bl_date", "etd"):
        value = getattr(data, field)
        if value is not None:
            setattr(s, field, value)

    if "transit_days" in fields_set:
        s.transit_days = data.transit_days

    # ETA: a truthy value is a human directly typing an arrival date — it locks the ETA
    # (eta_source='MANUAL') so the auto-formula backs off. An explicit null clears it back to
    # 'AUTO' and recalculates immediately. Editing etd or transit_days while eta_source is
    # still 'AUTO' (or unset) re-runs the formula so ETA tracks the latest inputs. Sending
    # eta_source="AUTO" explicitly is the "Reset to Estimate" action — hands a locked
    # (MANUAL/WEBSITE) ETA back to the formula. See Shipment.eta_source.
    recalc_eta = False
    if "eta" in fields_set:
        if data.eta is not None:
            s.eta = data.eta
            s.eta_source = "MANUAL"
        else:
            s.eta_source = "AUTO"
            recalc_eta = True
    elif "eta_source" in fields_set and data.eta_source == "AUTO":
        s.eta_source = "AUTO"
        recalc_eta = True
    elif s.eta_source in (None, "AUTO") and (
        "transit_days" in fields_set or ("etd" in fields_set and data.etd is not None)
    ):
        recalc_eta = True

    if recalc_eta:
        new_eta = estimate_eta(s.etd, s.transit_days)
        if new_eta:
            s.eta = new_eta
            s.eta_source = "AUTO"

    # Manual milestone dates — nullable, so an explicit key clears when empty. These drive
    # the automatic status derived below (intimation -> at bank, payment, original docs,
    # delivery -> delivered). retirement_date is kept for record only (no status effect).
    for field in ("payment_date", "original_doc_date", "intimation_date", "maturity_date",
                  "delivery_date", "retirement_date", "on_port_date", "departure_date"):
        if field in fields_set:
            setattr(s, field, getattr(data, field))
    if "departure_date" in fields_set and s.departure_date and not data.port_status:
        sync_bl_demurrage_from_departure(s, s.departure_date)
    if "exchange_rate" in fields_set:
        s.exchange_rate = data.exchange_rate

    # Manual vessel port-status override — a human correcting/overriding whatever the KPT
    # crawler last recorded (or filling it in when the crawler missed this vessel entirely).
    # apply_port_status() normalizes free text/KPT values into the canonical labels and
    # updates vessel_location/kpt_berth/on_port_date/departure_date together.
    if data.port_status:
        apply_port_status(
            s, data.port_status,
            berth=data.kpt_berth,
            on_port_date=data.on_port_date,
            departure_date=data.departure_date,
        )
        s.vessel_status_source = "MANUAL"
        s.vessel_status_updated_at = datetime.utcnow()


def _maybe_close_lc(s: Shipment, db: Session, user_id: int) -> None:
    """When every (non-deleted) shipment under an LC has reached DELIVERED, the LC is
    done — close it automatically rather than leaving it OPEN/SHIPPED forever waiting
    on a manual edit. No-op if the LC is already CLOSED/CANCELLED or still has
    un-delivered shipments."""
    lc = s.lc
    if not lc or (lc.status or "").upper() in ("CLOSED", "CANCELLED"):
        return
    siblings = db.query(Shipment).filter(
        Shipment.lc_id == lc.lc_id,
        Shipment.is_deleted.is_(False),
    ).all()
    if not siblings or any((sib.status or "") != "DELIVERED" for sib in siblings):
        return
    lc.status = "CLOSED"
    lc.status_changed_at = datetime.utcnow()
    lc.status_changed_by = user_id
    log_activity(db, s.shipment_id, user_id, "LC_AUTO_CLOSED",
                 detail=f"LC {lc.lc_number} auto-closed — all shipments delivered")


def update_shipment(shipment_id: int, data: ShipmentUpdate, db: Session, user_id: int) -> Shipment:
    s = get_shipment_or_404(shipment_id, db)
    old_status = s.status

    apply_shipment_fields(s, data)

    # Status is fully automatic — derive it from the (possibly just-updated) milestone
    # dates + documents. The most-advanced applicable stage wins.
    recompute_shipment_status(s)

    s.updated_by = user_id
    s.updated_at = datetime.utcnow()

    # Activity trail: record a status change distinctly, otherwise a generic detail update.
    if s.status != old_status:
        log_activity(db, shipment_id, user_id, "STATUS",
                     detail=f"Status {SHIPMENT_STATUS_LABEL.get(old_status or '', old_status)} "
                            f"→ {SHIPMENT_STATUS_LABEL.get(s.status or '', s.status)}")
    else:
        log_activity(db, shipment_id, user_id, "UPDATE", detail="Shipment details updated")

    if s.status == "DELIVERED" and s.status != old_status:
        _maybe_close_lc(s, db, user_id)

    from modules.shipments.demurrage_service import close_bl_demurrage_on_delivery
    close_bl_demurrage_on_delivery(s)

    db.commit()
    return s


def delete_shipment(shipment_id: int, db: Session, user_id: int) -> bool:
    """Soft delete. Returns True if this call actually deleted it, False if it was
    already deleted (idempotent, matches the original endpoint's `already_deleted` flag)."""
    s = get_shipment_or_404(shipment_id, db)
    if s.is_deleted:
        return False
    s.is_deleted = True
    s.deleted_at = datetime.utcnow()
    s.deleted_by = user_id
    log_activity(db, shipment_id, user_id, "DELETE", detail="Shipment deleted (soft)")
    db.commit()
    return True


def restore_shipment(shipment_id: int, db: Session, user_id: int) -> None:
    s = get_shipment_or_404(shipment_id, db)
    s.is_deleted = False
    s.deleted_at = None
    s.deleted_by = None
    log_activity(db, shipment_id, user_id, "RESTORE", detail="Shipment restored")
    db.commit()
