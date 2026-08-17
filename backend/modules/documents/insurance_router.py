"""
Marine Insurance API — upload, AI extract, verify against shipment, save, CRUD.
An insurance certificate/policy attaches to a shipment. PDF only. After extraction the
BL/LC numbers are cross-checked against the shipment's BL and LC (non-blocking warning).
"""

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
from utils.staging import stage_upload

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/insurance", tags=["Insurance"])

ALLOWED_EXTENSIONS = svc.ALLOWED_EXTENSIONS
STAGE_SUBDIR = svc.STAGE_SUBDIR

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

    existing = (db.query(InsuranceCertificate)
                  .filter(InsuranceCertificate.shipment_id == shipment_id)
                  .order_by(InsuranceCertificate.insurance_id.desc()).first())
    existing_dict = svc.to_dict(existing, shipment) if existing else {}
    existing_id = existing.insurance_id if existing else None

    try:
        staged_name, stage_path, _ = stage_upload(file, STAGE_SUBDIR, ALLOWED_EXTENSIONS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    extracted, extraction_error = safe_extract(
        extract_insurance, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Insurance, shipment_id={shipment_id}, file={file.filename}")

    if extraction_error:
        logger.warning(
            "Insurance shipment=%s: extraction failed, staged for manual entry (existing=%s).",
            shipment_id, existing_id,
        )
        return {
            "staged_file": staged_name,
            "original_filename": file.filename,
            "existing_id": existing_id,
            "extracted": existing_dict,
            "verification": svc.verify(None, None, shipment),
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": existing is not None,
        }

    meter_document_accepted(file_path=stage_path)
    verification = svc.verify(extracted.get("bl_number"), extracted.get("lc_number"), shipment)
    warnings = []
    if verification["bl"] == "NOT_MATCHED":
        warnings.append(f"Extracted BL number does not match the shipment BL "
                        f"({verification['shipment_bl_number'] or 'none on file'}).")
    if verification["lc"] == "NOT_MATCHED":
        warnings.append(f"Extracted LC number does not match the shipment LC "
                        f"({verification['shipment_lc_number'] or 'none on file'}).")

    return {
        "staged_file": staged_name,
        "original_filename": file.filename,
        "existing_id": existing_id,
        "extracted": extracted,
        "verification": verification,
        "warnings": warnings,
        "extraction_failed": False,
    }


from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def save_insurance(data: InsuranceSave, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    ins = svc.save_insurance(db, data, current_user.user_id)
    log_activity(db, ins.shipment_id, current_user.user_id, "UPLOAD", doc_type="Insurance")
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
