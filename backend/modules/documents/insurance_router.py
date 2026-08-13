"""
Marine Insurance API — upload, AI extract, verify against shipment, save, CRUD.
An insurance certificate/policy attaches to a shipment. PDF only. After extraction the
BL/LC numbers are cross-checked against the shipment's BL and LC (non-blocking warning).
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
from models.database_models import InsuranceCertificate, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.documents.extractors.insurance_extractor import extract_insurance
from infrastructure.activity.activity_service import log_activity
from modules.documents import insurance_service as svc
from modules.documents.insurance_schemas import InsuranceSave

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/insurance", tags=["Insurance"])

# Match the shared shipment upload component (JPG/PNG/PDF) so behaviour/validation
# messages are identical to the other document types.
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

    # One insurance certificate per shipment — reuse the existing row instead of
    # accumulating duplicates when the document is (re-)uploaded.
    ins = (db.query(InsuranceCertificate)
             .filter(InsuranceCertificate.shipment_id == shipment_id)
             .order_by(InsuranceCertificate.insurance_id.desc()).first())
    replacing = ins is not None
    if ins is None:
        ins = InsuranceCertificate(shipment_id=shipment_id, lc_id=shipment.lc_id,
                                   source="UPLOADED", status="PENDING_REVIEW",
                                   created_by=current_user.user_id)
        db.add(ins)
        db.flush()

    # Keep the old document + the already-verified column values until the new document
    # has actually been extracted.
    old_path = ins.document_path
    ins.document_path = svc.save_file(file, ins.insurance_id)
    ins.document_filename = file.filename
    db.commit()
    meter_document_accepted(file_path=ins.document_path)

    extracted, extraction_error = safe_extract(
        extract_insurance, ins.document_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Insurance, insurance_id={ins.insurance_id}, file={file.filename}")

    if extraction_error:
        log_activity(db, shipment_id, current_user.user_id,
                     "REPLACE" if replacing else "UPLOAD", doc_type="Insurance")
        db.commit()
        logger.warning(f"Insurance {ins.insurance_id}: extraction failed, falling back to "
                       f"manual entry (previous data preserved, replace={replacing}).")
        return {"insurance_id": ins.insurance_id, "document_filename": ins.document_filename,
                "extracted": svc.to_dict(ins) if replacing else {},
                "verification": svc.verify(None, None, shipment), "warnings": [],
                "extraction_failed": True, "extraction_message": extraction_error,
                "had_previous_data": replacing}

    if old_path and old_path != ins.document_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    ins.status = "PENDING_REVIEW"
    ins.source = "UPLOADED"
    ins.updated_by = current_user.user_id
    ins.raw_extracted_data = extracted
    # Pre-fill the record's columns from the extraction so the tab shows data immediately
    # even before the user explicitly saves (they can still edit/verify afterwards).
    svc.apply_insurance_fields(ins, InsuranceSave(**extracted), current_user.user_id)
    log_activity(db, shipment_id, current_user.user_id,
                 "REPLACE" if replacing else "UPLOAD", doc_type="Insurance")
    db.commit()
    db.refresh(ins)

    verification = svc.verify(extracted.get("bl_number"), extracted.get("lc_number"), shipment)
    warnings = []
    if verification["bl"] == "NOT_MATCHED":
        warnings.append(f"Extracted BL number does not match the shipment BL "
                        f"({verification['shipment_bl_number'] or 'none on file'}).")
    if verification["lc"] == "NOT_MATCHED":
        warnings.append(f"Extracted LC number does not match the shipment LC "
                        f"({verification['shipment_lc_number'] or 'none on file'}).")

    return {"insurance_id": ins.insurance_id, "document_filename": ins.document_filename,
            "extracted": extracted, "verification": verification, "warnings": warnings,
            "extraction_failed": False}


from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def save_insurance(data: InsuranceSave, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    ins = svc.save_insurance(db, data, current_user.user_id)
    log_activity(db, ins.shipment_id, current_user.user_id, "EDIT", doc_type="Insurance")
    validate_shipment(ins.shipment_id, db)
    db.commit()
    db.refresh(ins)
    logger.info(f"Insurance saved: id={ins.insurance_id}, cert={ins.certificate_number}")
    return {"success": True, "insurance_id": ins.insurance_id}


@router.get("/by-shipment/{shipment_id}")
def insurance_for_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                           current_user: User = Depends(get_current_user)):
    shipment = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    rows = db.query(InsuranceCertificate).filter(
        InsuranceCertificate.shipment_id == shipment_id).all()
    return {"shipment_id": shipment_id, "count": len(rows),
            "items": [svc.to_dict(r, shipment) for r in rows]}


@router.get("/{insurance_id}")
def get_insurance(insurance_id: int, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(get_current_user)):
    ins = svc.get_insurance_or_404(db, insurance_id)
    shipment = (db.query(Shipment).filter(Shipment.shipment_id == ins.shipment_id).first()
                if ins.shipment_id else None)
    return svc.to_dict(ins, shipment)


@router.put("/{insurance_id}")
def update_insurance(insurance_id: int, data: InsuranceSave, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(_can_write)):
    ins = svc.update_insurance(db, insurance_id, data, current_user.user_id)
    log_activity(db, ins.shipment_id, current_user.user_id, "EDIT", doc_type="Insurance")
    validate_shipment(ins.shipment_id, db)
    db.commit()
    return {"success": True, "insurance_id": insurance_id}


@router.delete("/{insurance_id}")
def delete_insurance(insurance_id: int, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(_can_write)):
    ins = svc.delete_insurance(db, insurance_id)
    db.commit()
    return {"success": True, "insurance_id": insurance_id}


@router.get("/{insurance_id}/document")
def get_document(insurance_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    ins = svc.get_insurance_or_404(db, insurance_id)
    if not ins.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this Insurance Certificate")
    return document_file_response(ins.document_path, ins.document_filename)
