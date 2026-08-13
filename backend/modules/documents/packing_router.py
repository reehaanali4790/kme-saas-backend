"""
Packing List API — upload, AI extract, verify/save (with line items), CRUD.
Packing lists attach to a shipment.
"""

import os
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from core.permissions import require_min_role
from models.database_models import PackingList, PackingLineItem, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.activity.activity_service import log_activity
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.documents.extractors.packing_extractor import extract_packing
from modules.documents import packing_service as svc
from modules.documents.packing_schemas import PackingSave

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/packing", tags=["Packing Lists"])

ALLOWED_EXTENSIONS = svc.ALLOWED_EXTENSIONS

_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


@router.post("/upload-and-extract")
def upload_and_extract(
    shipment_id: int = Query(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
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

    # One packing list per shipment — reuse the existing record instead of piling up
    # duplicate / abandoned-placeholder rows when a document is (re-)uploaded.
    p = (db.query(PackingList)
           .filter(PackingList.shipment_id == shipment_id)
           .order_by(PackingList.packing_id.desc()).first())
    is_replace = p is not None
    if p is None:
        p = PackingList(shipment_id=shipment_id, source="UPLOADED",
                        status="PENDING_REVIEW", created_by=current_user.user_id)
        db.add(p)
        db.flush()

    # Save the new document first; keep the old line items + old file until the extraction
    # has actually succeeded, so a failed extraction can't destroy the last good data.
    old_path = p.document_path
    p.document_path = svc.save_file(file, p.packing_id)
    p.document_filename = file.filename
    db.commit()
    meter_document_accepted(file_path=p.document_path)

    extracted, extraction_error = safe_extract(
        extract_packing, p.document_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Packing List, packing_id={p.packing_id}, file={file.filename}")

    if extraction_error:
        db.commit()
        logger.warning(f"Packing {p.packing_id}: extraction failed, falling back to manual "
                       f"entry (previous data preserved, replace={is_replace}).")
        return {"packing_id": p.packing_id, "document_filename": p.document_filename,
                "is_pdf": ext == ".pdf",
                "extracted": svc.to_dict(p) if is_replace else {},
                "extraction_failed": True, "extraction_message": extraction_error,
                "had_previous_data": is_replace}

    # Extraction worked — now the new upload can replace the prior extraction.
    db.query(PackingLineItem).filter(PackingLineItem.packing_id == p.packing_id).delete()
    if old_path and old_path != p.document_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    p.status = "PENDING_REVIEW"
    p.source = "UPLOADED"
    p.updated_by = current_user.user_id
    p.raw_extracted_data = extracted
    db.commit()

    partial = bool(extracted.get("_extraction_partial"))
    return {"packing_id": p.packing_id, "document_filename": p.document_filename,
            "is_pdf": ext == ".pdf", "extracted": extracted,
            "extraction_failed": False, "extraction_partial": partial,
            "extraction_message": (
                "This document was long, so some line items may be missing. "
                "Please check the rows below before saving." if partial else None)}


from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def save_packing(data: PackingSave, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(_can_write)):
    p = svc.save_packing(db, data, current_user.user_id)
    log_activity(db, p.shipment_id, current_user.user_id, "UPLOAD", doc_type="Packing List")
    validate_shipment(p.shipment_id, db)
    db.commit()
    db.refresh(p)
    logger.info(f"Packing saved: id={p.packing_id}, number={p.packing_number}")
    return {"success": True, "packing_id": p.packing_id}


@router.get("/{packing_id}")
def get_packing(packing_id: int, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(get_current_user)):
    p = svc.get_packing_or_404(db, packing_id, with_items=True)
    return svc.to_dict(p)


@router.put("/{packing_id}")
def update_packing(packing_id: int, data: PackingSave, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    p = svc.update_packing(db, packing_id, data, current_user.user_id)
    log_activity(db, p.shipment_id, current_user.user_id, "EDIT", doc_type="Packing List")
    validate_shipment(p.shipment_id, db)
    db.commit()
    return {"success": True, "packing_id": packing_id}


@router.delete("/{packing_id}")
def delete_packing(packing_id: int, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    p = svc.delete_packing(db, packing_id)
    db.commit()
    return {"success": True, "packing_id": packing_id}


@router.get("/{packing_id}/document")
def get_document(packing_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    p = svc.get_packing_or_404(db, packing_id)
    if not p.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this Packing List")
    return document_file_response(p.document_path, p.document_filename)
