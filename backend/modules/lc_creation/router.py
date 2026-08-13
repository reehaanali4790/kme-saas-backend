"""
LC Creation Router — API endpoints for LC Creation
"""
import os
import uuid
import shutil
import logging
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from models.database_models import LCMaster, Contract, User
from modules.auth.dependencies import get_current_user
from core.permissions import require_permission
from modules.lc_creation.extractors.lc_extractor import extract_lc
from modules.lc_creation.helpers.shipment_validator import cross_check_lc_vs_contract
from infrastructure.document_ai.document_ai import ExtractionError
from core.platform_metering import enforce_document_quota, meter_document_accepted
from . import services as svc
from .schemas import LCCreate, LCCreateResult

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/lc-create", tags=["LC Creation"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "lc_documents")
STAGE_DIR = os.path.join(UPLOAD_DIR, "_staged")


def _safe_remove(p):
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass


@router.post("/upload-and-extract")
def upload_and_extract(
    contract_id: Optional[int] = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("import_lc")),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not set up on this server. Please contact support, or enter the details manually.")

    enforce_document_quota()

    contract = None
    if contract_id:
        contract = db.query(Contract).filter(Contract.contract_id == contract_id).first()
        if not contract:
            raise HTTPException(status_code=404, detail="Contract not found")

    os.makedirs(STAGE_DIR, exist_ok=True)
    staged = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(STAGE_DIR, staged)
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        extracted = extract_lc(path, settings.ANTHROPIC_API_KEY)
    except ExtractionError as e:
        _safe_remove(path)
        logger.error(f"LC extraction failed for {file.filename}: {e.detail}")
        raise HTTPException(status_code=422, detail=e.user_message)
    except Exception as e:
        _safe_remove(path)
        logger.error(f"LC extraction failed for {file.filename}: {type(e).__name__}: {e}",
                     exc_info=True)
        raise HTTPException(
            status_code=422,
            detail="Could not read this LC document automatically. Please try a clearer "
                   "file, or enter the LC details manually.")

    warnings = cross_check_lc_vs_contract(extracted, contract_id, db) if contract_id else []
    if extracted.get("is_lc") is False:
        detected = extracted.get("document_type") or "a different document"
        warnings.insert(0, f"This file may not be a Letter of Credit — it looks like {detected}. "
                           f"Check the extracted fields below before creating the LC.")
    if warnings:
        logger.warning(f"LC cross-check vs contract {contract_id}: {warnings}")
    meter_document_accepted(file_path=path)
    return {"staged_file": staged, "original_filename": file.filename,
            "is_pdf": ext == ".pdf", "extracted": extracted, "warnings": warnings,
            "contract_id": contract_id}


@router.post("/", response_model=LCCreateResult)
def create_lc(data: LCCreate, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(require_permission("import_lc"))):
    lc, warnings, shipment_id = svc.create_lc(db, data, created_by=current_user.user_id)
    return LCCreateResult(lc_id=lc.lc_id, lc_number=lc.lc_number, warnings=warnings,
                          shipment_id=shipment_id)


@router.get("/{lc_id}/document")
def get_document(lc_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc or not lc.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this LC")
    return document_file_response(lc.document_path, os.path.basename(lc.document_path))
