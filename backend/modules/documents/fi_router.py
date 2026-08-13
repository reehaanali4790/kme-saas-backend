"""
Financial Instrument (FI) API — upload, AI extract, verify/save, CRUD.
FI attaches to a shipment. Carries the HS code (cross-checked) and the expiry date
(= last date to file the GD), which drives the FI_EXPIRY alert.
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
from models.database_models import FinancialInstrument, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.activity.activity_service import log_activity
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.documents.extractors.fi_extractor import extract_fi
from modules.documents import fi_service as svc
from modules.documents.fi_schemas import FISave

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/fi", tags=["Financial Instruments"])

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

    # One FI per shipment — reuse the existing record instead of piling up
    # duplicate / abandoned-placeholder rows when a document is (re-)uploaded.
    fi = (db.query(FinancialInstrument)
            .filter(FinancialInstrument.shipment_id == shipment_id)
            .order_by(FinancialInstrument.fi_id.desc()).first())
    is_replace = fi is not None
    if fi is None:
        fi = FinancialInstrument(shipment_id=shipment_id, lc_id=shipment.lc_id,
                                 source="UPLOADED", status="PENDING_REVIEW",
                                 created_by=current_user.user_id)
        db.add(fi)
        db.flush()

    # Keep the old document until the new one has actually been extracted.
    old_path = fi.document_path
    fi.document_path = svc.save_file(file, fi.fi_id)
    fi.document_filename = file.filename
    db.commit()
    meter_document_accepted(file_path=fi.document_path)

    extracted, extraction_error = safe_extract(
        extract_fi, fi.document_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Financial Instrument, fi_id={fi.fi_id}, file={file.filename}")

    if extraction_error:
        db.commit()
        logger.warning(f"FI {fi.fi_id}: extraction failed, falling back to manual entry "
                       f"(previous data preserved, replace={is_replace}).")
        return {"fi_id": fi.fi_id, "document_filename": fi.document_filename,
                "is_pdf": ext == ".pdf",
                "extracted": svc.to_dict(fi) if is_replace else {},
                "extraction_failed": True, "extraction_message": extraction_error,
                "had_previous_data": is_replace, "warnings": []}

    if old_path and old_path != fi.document_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    fi.status = "PENDING_REVIEW"
    fi.source = "UPLOADED"
    fi.updated_by = current_user.user_id
    fi.raw_extracted_data = extracted
    db.commit()

    warnings = svc.check_expiry_warning(extracted)

    return {"fi_id": fi.fi_id, "document_filename": fi.document_filename,
            "is_pdf": ext == ".pdf", "extracted": extracted, "warnings": warnings,
            "extraction_failed": False}


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
