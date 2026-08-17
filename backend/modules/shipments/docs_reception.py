"""Compute shipment docs_reception_status from path-aware required docs."""
from __future__ import annotations

from typing import Optional

from models.database_models import Shipment
from modules.shipments import services as ship_svc
from modules.shipments.vessel_status_service import resolve_vessel_status
from modules.workflow.import_paths import (
    DOCS_RECEPTION_AWAITING,
    DOCS_RECEPTION_COMPLETE,
    DOCS_RECEPTION_NOT_STARTED,
    DOCS_RECEPTION_PARTIAL,
    missing_required_docs,
    normalize_import_mode,
)


def _doc_present(records) -> bool:
    return bool(records)


def _on_port(shipment: Shipment) -> bool:
    vs = resolve_vessel_status(shipment)
    status = (vs.get("vessel_status") or "").lower()
    if "on port" in status or "berth" in status:
        return True
    if shipment.on_port_date:
        return True
    if vs.get("on_port_date"):
        return True
    return False


def compute_docs_reception_status(shipment: Shipment) -> str:
    mode = normalize_import_mode(getattr(shipment, "import_mode", None))
    has_bl = _doc_present(shipment.bill_of_ladings)
    has_inv = _doc_present(shipment.commercial_invoices)
    has_pkg = _doc_present(shipment.packing_lists)
    has_fi = _doc_present(shipment.financial_instruments)

    any_stub = has_bl or has_inv or has_pkg or has_fi
    if not any_stub:
        return DOCS_RECEPTION_NOT_STARTED

    missing = missing_required_docs(
        mode,
        has_bl=has_bl,
        has_invoice=has_inv,
        has_packing=has_pkg,
        has_fi=has_fi,
    )
    if not missing:
        return DOCS_RECEPTION_COMPLETE

    if _on_port(shipment):
        return DOCS_RECEPTION_AWAITING
    return DOCS_RECEPTION_PARTIAL


def recompute_docs_reception_status(shipment: Shipment, db) -> str:
    status = compute_docs_reception_status(shipment)
    if shipment.docs_reception_status != status:
        shipment.docs_reception_status = status
        db.flush()
    return status


def docs_reception_summary(shipment: Shipment) -> dict:
    mode = normalize_import_mode(getattr(shipment, "import_mode", None))
    missing = missing_required_docs(
        mode,
        has_bl=_doc_present(shipment.bill_of_ladings),
        has_invoice=_doc_present(shipment.commercial_invoices),
        has_packing=_doc_present(shipment.packing_lists),
        has_fi=_doc_present(shipment.financial_instruments),
    )
    pending_files: list[str] = []
    for label, records in (
        ("BL", shipment.bill_of_ladings),
        ("Invoice", shipment.commercial_invoices),
        ("Packing", shipment.packing_lists),
        ("FI", shipment.financial_instruments),
        ("Insurance", shipment.insurance_certificates),
    ):
        for rec in records or []:
            if getattr(rec, "file_pending", False):
                pending_files.append(label)
                break

    return {
        "docs_reception_status": shipment.docs_reception_status or DOCS_RECEPTION_NOT_STARTED,
        "missing_required_docs": missing,
        "manual_stubs_pending_file": pending_files,
        "on_port": _on_port(shipment),
    }
