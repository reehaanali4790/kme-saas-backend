"""
Commercial Invoice API — upload, AI extract, verify/save (with line items), CRUD.
Invoices attach to a shipment. Saving a verified invoice updates the shipment's
delivered quantity / totals and verifies the documentary credit number against the LC.
"""

import os
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from core.permissions import require_min_role
from models.database_models import CommercialInvoice, InvoiceLineItem, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.activity.activity_service import log_activity
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.documents.extractors.invoice_extractor import extract_invoice
from modules.documents import invoice_service as svc
from modules.documents.invoice_schemas import InvoiceSave
from modules.workflow.helpers import check_gate
from modules.workflow.constants import ACTION_UPLOAD_INVOICE

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/invoices", tags=["Commercial Invoices"])

ALLOWED_EXTENSIONS = svc.ALLOWED_EXTENSIONS

_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


@router.post("/upload-and-extract")
def upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_INVOICE,
               user_id=current_user.user_id, override_reason=override_reason)
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not set up on this server. Please contact support, or enter the details manually.")

    enforce_document_quota()

    shipment = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    # One invoice per shipment — reuse the existing record instead of piling up
    # duplicate / abandoned-placeholder rows when a document is (re-)uploaded.
    inv = (db.query(CommercialInvoice)
             .filter(CommercialInvoice.shipment_id == shipment_id)
             .order_by(CommercialInvoice.invoice_id.desc()).first())
    is_replace = inv is not None
    if inv is None:
        inv = CommercialInvoice(shipment_id=shipment_id, lc_id=shipment.lc_id,
                                source="UPLOADED", status="PENDING_REVIEW",
                                created_by=current_user.user_id)
        db.add(inv)
        db.flush()

    # Save the new document FIRST and keep the old one until we know the extraction worked.
    # (The previous order wiped the old line items before extracting, so a failed extraction
    # destroyed the last good data.)
    old_path = inv.document_path
    inv.document_path = svc.save_file(file, inv.invoice_id)
    inv.document_filename = file.filename
    db.commit()
    meter_document_accepted(file_path=inv.document_path)

    extracted, extraction_error = safe_extract(
        extract_invoice, inv.document_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Commercial Invoice, invoice_id={inv.invoice_id}, file={file.filename}")

    if extraction_error:
        # The file is saved and is now the invoice's document, but nothing else is touched:
        # any previously verified header fields + line items stay exactly as they were, and
        # are sent back so the user can review/correct them by hand instead of starting over.
        db.commit()
        logger.warning(f"Invoice {inv.invoice_id}: extraction failed, falling back to manual "
                       f"entry (previous data preserved, replace={is_replace}).")
        existing = svc.to_dict(inv) if is_replace else {}
        return {"invoice_id": inv.invoice_id, "document_filename": inv.document_filename,
                "is_pdf": ext == ".pdf", "extracted": existing,
                "extraction_failed": True, "extraction_message": extraction_error,
                "had_previous_data": is_replace}

    # Extraction succeeded — now it's safe to clear the old line items and the old file.
    db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == inv.invoice_id).delete()
    if old_path and old_path != inv.document_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    inv.status = "PENDING_REVIEW"
    inv.source = "UPLOADED"
    inv.updated_by = current_user.user_id
    inv.raw_extracted_data = extracted
    db.commit()

    partial = bool(extracted.get("_extraction_partial"))
    return {"invoice_id": inv.invoice_id, "document_filename": inv.document_filename,
            "is_pdf": ext == ".pdf", "extracted": extracted,
            "extraction_failed": False,
            "extraction_partial": partial,
            "extraction_message": (
                "This document was long, so some line items may be missing. "
                "Please check the rows below before saving." if partial else None)}


from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def save_invoice(data: InvoiceSave, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(_can_write)):
    inv = svc.save_invoice(db, data, current_user.user_id)
    warning = svc.sync_shipment_from_invoice(inv, db)
    log_activity(db, inv.shipment_id, current_user.user_id, "UPLOAD", doc_type="Commercial Invoice")
    validate_shipment(inv.shipment_id, db)
    db.commit()
    db.refresh(inv)
    logger.info(f"Invoice saved: id={inv.invoice_id}, number={inv.invoice_number}")
    return {"success": True, "invoice_id": inv.invoice_id, "warning": warning}


@router.get("/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(get_current_user)):
    inv = svc.get_invoice_or_404(db, invoice_id, with_items=True)
    return svc.to_dict(inv)


@router.put("/{invoice_id}")
def update_invoice(invoice_id: int, data: InvoiceSave, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    inv = svc.update_invoice(db, invoice_id, data, current_user.user_id)
    warning = svc.sync_shipment_from_invoice(inv, db)
    log_activity(db, inv.shipment_id, current_user.user_id, "EDIT", doc_type="Commercial Invoice")
    validate_shipment(inv.shipment_id, db)
    db.commit()
    return {"success": True, "invoice_id": invoice_id, "warning": warning}


@router.delete("/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    inv = svc.delete_invoice(db, invoice_id)
    db.commit()
    return {"success": True, "invoice_id": invoice_id}


@router.get("/{invoice_id}/document")
def get_document(invoice_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    inv = svc.get_invoice_or_404(db, invoice_id)
    if not inv.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this Commercial Invoice")
    return document_file_response(inv.document_path, inv.document_filename)
