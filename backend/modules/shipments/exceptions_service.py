"""Shipment exception queues for ops visibility."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session, joinedload, selectinload

from models.database_models import LCMaster, Shipment
from modules.shipments.docs_reception import docs_reception_summary
from modules.shipments.vessel_status_service import resolve_vessel_status
from modules.workflow.import_paths import (
    DOCS_RECEPTION_AWAITING,
    DOCS_RECEPTION_PARTIAL,
    is_lc_backed,
    normalize_import_mode,
)


def _base_query(db: Session):
    return db.query(Shipment).filter(Shipment.is_deleted.is_(False)).options(
        joinedload(Shipment.lc),
        selectinload(Shipment.bill_of_ladings),
        selectinload(Shipment.commercial_invoices),
        selectinload(Shipment.packing_lists),
        selectinload(Shipment.financial_instruments),
        selectinload(Shipment.insurance_certificates),
        selectinload(Shipment.validations),
    )


def _shipment_exception_item(s: Shipment) -> dict:
    summary = docs_reception_summary(s)
    vs = resolve_vessel_status(s)
    return {
        "shipment_id": s.shipment_id,
        "shipment_ref": s.shipment_ref,
        "lc_id": s.lc_id,
        "lc_number": s.lc.lc_number if s.lc else None,
        "contract_id": s.contract_id,
        "import_mode": normalize_import_mode(s.import_mode),
        "vessel_name": s.vessel_name,
        "eta": s.eta.isoformat() if s.eta else None,
        "vessel_status": vs.get("vessel_status"),
        "on_port_date": vs.get("on_port_date"),
        "docs_reception_status": s.docs_reception_status,
        "missing_required_docs": summary.get("missing_required_docs") or [],
        "manual_stubs_pending_file": summary.get("manual_stubs_pending_file") or [],
        "validation_status": s.validation_status,
    }


def list_exceptions(db: Session, *, queue: Optional[str] = None, limit: int = 50) -> dict:
    today = date.today()
    shipments = _base_query(db).order_by(Shipment.eta.asc().nullslast()).limit(500).all()

    landed_awaiting: list[dict] = []
    partial_docs: list[dict] = []
    manual_pending: list[dict] = []
    non_lc_active: list[dict] = []
    near_eta_no_bl: list[dict] = []
    validation_blocked_on_port: list[dict] = []

    for s in shipments:
        item = _shipment_exception_item(s)
        summary = docs_reception_summary(s)
        mode = normalize_import_mode(s.import_mode)
        has_bl = bool(s.bill_of_ladings)
        on_port = summary.get("on_port")
        status = s.docs_reception_status or ""

        if status == DOCS_RECEPTION_AWAITING and on_port:
            landed_awaiting.append(item)
        if status == DOCS_RECEPTION_PARTIAL:
            partial_docs.append(item)
        if summary.get("manual_stubs_pending_file"):
            manual_pending.append(item)
        if not is_lc_backed(mode):
            non_lc_active.append(item)
        if s.eta and not has_bl and (s.eta - today).days <= 7:
            near_eta_no_bl.append(item)
        if on_port and (s.validation_status or "").upper() in ("DISCREPANT", "FAIL"):
            validation_blocked_on_port.append(item)
        elif on_port:
            fails = [v for v in (s.validations or []) if (v.status or "").upper() == "FAIL"]
            if fails:
                validation_blocked_on_port.append(item)

    queues = {
        "landed_awaiting_docs": landed_awaiting[:limit],
        "partial_documentation": partial_docs[:limit],
        "manual_stubs_pending_file": manual_pending[:limit],
        "non_lc_active": non_lc_active[:limit],
        "near_eta_no_bl": near_eta_no_bl[:limit],
        "validation_blocked_on_port": validation_blocked_on_port[:limit],
    }
    if queue:
        key = queue.strip().lower()
        alias = {
            "landed": "landed_awaiting_docs",
            "partial": "partial_documentation",
            "manual": "manual_stubs_pending_file",
            "non_lc": "non_lc_active",
            "eta": "near_eta_no_bl",
            "validation": "validation_blocked_on_port",
        }.get(key, key)
        return {"queue": alias, "items": queues.get(alias, [])[:limit], "count": len(queues.get(alias, []))}
    return {"queues": {k: {"count": len(v), "items": v[:limit]} for k, v in queues.items()}}
