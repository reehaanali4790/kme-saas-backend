"""
Financial Instrument (FI) API — upload, AI extract, verify/save, CRUD.
FI attaches to a shipment. Carries the HS code (cross-checked) and the expiry date
(= last date to file the GD), which drives the FI_EXPIRY alert.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from core.permissions import require_min_role
from models.database_models import FinancialInstrument, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.activity.activity_service import log_activity
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.documents.extractors.fi_extractor import extract_fi
from modules.documents import fi_service as svc
from modules.documents.fi_schemas import FISave
from modules.workflow.helpers import check_gate
from modules.workflow.constants import ACTION_UPLOAD_FI
from utils.staging import stage_upload

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/fi", tags=["Financial Instruments"])

ALLOWED_EXTENSIONS = svc.ALLOWED_EXTENSIONS
STAGE_SUBDIR = svc.STAGE_SUBDIR

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
    check_gate(db, request, shipment_id, ACTION_UPLOAD_FI,
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

    existing = (db.query(FinancialInstrument)
                  .filter(FinancialInstrument.shipment_id == shipment_id)
                  .order_by(FinancialInstrument.fi_id.desc()).first())
    existing_dict = svc.to_dict(existing) if existing else {}
    existing_id = existing.fi_id if existing else None

    try:
        staged_name, stage_path, _ = stage_upload(file, STAGE_SUBDIR, ALLOWED_EXTENSIONS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    extracted, extraction_error = safe_extract(
        extract_fi, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Financial Instrument, shipment_id={shipment_id}, file={file.filename}")

    if extraction_error:
        logger.warning(
            "FI shipment=%s: extraction failed, staged for manual entry (existing=%s).",
            shipment_id, existing_id,
        )
        return {
            "staged_file": staged_name,
            "original_filename": file.filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": existing_dict,
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": existing is not None,
            "warnings": [],
        }

    meter_document_accepted(file_path=stage_path)
    warnings = svc.check_expiry_warning(extracted)

    return {
        "staged_file": staged_name,
        "original_filename": file.filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
        "extraction_failed": False,
    }


from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def save_fi(data: FISave, db: Session = Depends(get_tenant_db),
            current_user: User = Depends(_can_write)):
    fi = svc.save_fi(db, data, current_user.user_id)
    log_activity(db, fi.shipment_id, current_user.user_id, "UPLOAD", doc_type="Financial Instrument")
    validate_shipment(fi.shipment_id, db)
    db.commit()
    db.refresh(fi)
    logger.info(f"FI saved: id={fi.fi_id}, number={fi.fi_number}, expiry={fi.expiry_date}")
    return {"success": True, "fi_id": fi.fi_id}


@router.get("/by-shipment/{shipment_id}")
def fi_for_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(get_current_user)):
    fis = db.query(FinancialInstrument).filter(
        FinancialInstrument.shipment_id == shipment_id).all()
    return {"shipment_id": shipment_id, "count": len(fis), "items": [svc.to_dict(f) for f in fis]}


@router.get("/{fi_id}")
def get_fi(fi_id: int, db: Session = Depends(get_tenant_db),
           current_user: User = Depends(get_current_user)):
    fi = svc.get_fi_or_404(db, fi_id)
    return svc.to_dict(fi)


@router.put("/{fi_id}")
def update_fi(fi_id: int, data: FISave, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(_can_write)):
    fi = svc.update_fi(db, fi_id, data, current_user.user_id)
    log_activity(db, fi.shipment_id, current_user.user_id, "EDIT", doc_type="Financial Instrument")
    validate_shipment(fi.shipment_id, db)
    db.commit()
    return {"success": True, "fi_id": fi_id}


@router.delete("/{fi_id}")
def delete_fi(fi_id: int, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(_can_write)):
    fi = svc.delete_fi(db, fi_id)
    db.commit()
    return {"success": True, "fi_id": fi_id}


@router.get("/{fi_id}/document")
def get_document(fi_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    fi = svc.get_fi_or_404(db, fi_id)
    if not fi.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this Financial Instrument")
    return document_file_response(fi.document_path, fi.document_filename)
