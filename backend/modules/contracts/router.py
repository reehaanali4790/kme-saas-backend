"""
Contract Router — API endpoints for Contracts
"""
import os
import uuid
import shutil
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db, get_tenant_context, TenantContext
from core.platform_metering import (
    enforce_document_quota,
    meter_document_accepted,
    set_metering_org,
    clear_metering_org,
)
from config.settings import settings
from models.database_models import User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from modules.contracts.extractors.contract_extractor import extract_contract
from infrastructure.document_ai.document_ai import ExtractionError
from . import services as svc
from .schemas import (
    BankPerformanceRow, ContractOut, ContractSave, ContractStatusUpdate, SaveResult, StatusResult,
)

logger = logging.getLogger("uvicorn")

from utils.staging import staged_dir, upload_dir as staging_upload_dir

router = APIRouter(prefix="/api/contracts", tags=["Contracts"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
CONTRACT_SUBDIR = "contract_documents"


def _contract_stage_dir() -> str:
    return staged_dir(CONTRACT_SUBDIR)

_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


# ---------------------------------------------------------------------------
# Upload + extract
# ---------------------------------------------------------------------------
@router.post("/upload-and-extract")
def upload_and_extract(
    lc_id: int = Query(None),
    file: UploadFile = File(...),
    current_user: User = Depends(_can_write),
    tenant: TenantContext = Depends(get_tenant_context),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not set up on this server. Please contact support, or enter the details manually.")

    enforce_document_quota(tenant.organization_id)
    set_metering_org(tenant.organization_id)
    try:
        return _contract_upload_inner(lc_id, file, ext)
    finally:
        clear_metering_org()


def _contract_upload_inner(lc_id, file, ext):
    # Stage the file and verify it's actually a contract. Nothing is written to the
    # database here — extraction attempts (retries, abandoned uploads, navigating away)
    # must not leave permanent DRAFT rows behind. The contract is only created when the
    # user reviews the extracted fields and clicks "Save Contract" (see save_contract()),
    # same pattern as LC creation's upload-and-extract.
    os.makedirs(_contract_stage_dir(), exist_ok=True)
    staged = f"{uuid.uuid4().hex}{ext}"
    stage_path = os.path.join(_contract_stage_dir(), staged)
    with open(stage_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        extracted = extract_contract(stage_path, settings.ANTHROPIC_API_KEY)
    except ExtractionError as e:
        svc.safe_remove(stage_path)
        logger.error(f"Contract extraction failed for {file.filename}: {e.detail}")
        raise HTTPException(status_code=422, detail=e.user_message)
    except Exception as e:
        svc.safe_remove(stage_path)
        logger.error(f"Contract extraction failed for {file.filename}: {type(e).__name__}: {e}",
                     exc_info=True)
        raise HTTPException(
            status_code=422,
            detail="Could not read this contract automatically. Please try a clearer file, "
                   "or enter the contract details manually.")

    if extracted.get("is_contract") is False:
        svc.safe_remove(stage_path)
        detected = extracted.get("document_type") or "a different document"
        raise HTTPException(status_code=422,
                            detail=f"This file does not look like a contract — it appears to be {detected}. "
                                   f"Please upload a supplier purchase / sales contract.")

    meter_document_accepted(file_path=stage_path)
    return {"staged_file": staged, "original_filename": file.filename,
            "is_pdf": ext == ".pdf", "extracted": extracted, "lc_id": lc_id}


# ---------------------------------------------------------------------------
# Save (create / update placeholder)
# ---------------------------------------------------------------------------
@router.post("/", response_model=SaveResult)
def save_contract(data: ContractSave, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(_can_write)):
    try:
        c = svc.save_contract(db, data, created_by=current_user.user_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Contract save failed: %s: %s (contract_number=%s staged=%s)",
            type(e).__name__, e, data.contract_number, data.staged_file,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Could not save contract: {type(e).__name__}: {e}",
        ) from e
    logger.info(f"Contract saved: id={c.contract_id}, number={c.contract_number}, status={c.status}")
    return SaveResult(contract_id=c.contract_id)


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------
@router.get("/", response_model=list[ContractOut])
def list_contracts(status: str = Query(None), db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(get_current_user)):
    return [svc.to_schema(c) for c in svc.list_contracts(db, status)]


@router.get("/bank-performance", response_model=list[BankPerformanceRow])
def bank_performance(
    date_from: str = Query(None), date_to: str = Query(None), limit: int = Query(None),
    db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user),
):
    """Each bank's historical average LC issuance turnaround (days), across every
    completed contract. Registered before /{contract_id} so "bank-performance" is never
    swallowed as a contract_id path param."""
    return svc.bank_issuance_performance(db, date_from, date_to, limit)


@router.get("/lookup")
def lookup_contracts(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50),
                     db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(get_current_user)):
    return {"query": q, "items": svc.lookup_contracts(db, q, limit)}


@router.get("/{contract_id}", response_model=ContractOut)
def get_contract(contract_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    return svc.to_schema(svc.get_contract_or_404(db, contract_id))


@router.put("/{contract_id}", response_model=SaveResult)
def update_contract(contract_id: int, data: ContractSave, db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(_can_write)):
    svc.update_contract(db, contract_id, data, updated_by=current_user.user_id)
    return SaveResult(contract_id=contract_id)


@router.put("/{contract_id}/status", response_model=StatusResult)
def set_status(contract_id: int, data: ContractStatusUpdate, db: Session = Depends(get_tenant_db),
               current_user: User = Depends(_can_write)):
    c = svc.set_status(db, contract_id, data.status, updated_by=current_user.user_id)
    return StatusResult(contract_id=contract_id, status=c.status or "")


@router.get("/{contract_id}/document")
def get_document(contract_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    c = svc.get_contract_or_404(db, contract_id)
    if not c.document_path or not os.path.exists(c.document_path):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(c.document_path, filename=c.document_filename)


@router.delete("/{contract_id}")
def delete_contract(contract_id: int, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    svc.delete_contract(db, contract_id, deleted_by=current_user.user_id)
    return {"success": True, "contract_id": contract_id}
