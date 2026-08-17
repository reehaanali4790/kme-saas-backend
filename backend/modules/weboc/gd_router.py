"""
Goods Declaration API — upload, AI extract, verify/save, CRUD. Record-only.
GD attaches to a shipment.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from models.database_models import GoodsDeclaration, Shipment, User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from infrastructure.activity.activity_service import log_activity
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.weboc.extractors.gd_extractor_service import extract_gd
from modules.lc_creation.helpers.shipment_validator import cross_check_gd_extracted
from modules.weboc import gd_service as svc
from modules.weboc.gd_schemas import GDSave, GDSaveResult, GDStatusUpdate
from modules.workflow.helpers import check_gate
from modules.workflow.constants import ACTION_UPLOAD_GD, ACTION_GD_ADVANCE, ACTION_GD_SET_STATUS

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/gd", tags=["Goods Declarations"])

ALLOWED_EXTENSIONS = svc.ALLOWED_EXTENSIONS

# Mutations require OPERATOR+ - VIEWER can read GDs but not create/edit/advance them.
_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")

# Re-exported for backward-compat call sites (modules/weboc/services.py used to import
# these from this module directly - they now live in gd_service.py).
recompute_gd_status = svc.recompute_gd_status
gd_is_closed = svc.gd_is_closed


@router.post("/upload-and-extract")
def upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_GD,
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

    existing = (db.query(GoodsDeclaration)
                  .filter(GoodsDeclaration.shipment_id == shipment_id)
                  .order_by(GoodsDeclaration.gd_id.desc()).first())
    existing_dict = svc.to_response(existing, db) if existing else {}
    existing_id = existing.gd_id if existing else None

    try:
        from utils.staging import stage_upload
        staged_name, stage_path, _ = stage_upload(file, svc.STAGE_SUBDIR, ALLOWED_EXTENSIONS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    extracted, extraction_error = safe_extract(
        extract_gd, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Goods Declaration, shipment_id={shipment_id}, file={file.filename}")

    if extraction_error:
        logger.warning(
            "GD shipment=%s: extraction failed, staged for manual entry (existing=%s).",
            shipment_id, existing_id,
        )
        return {
            "staged_file": staged_name,
            "original_filename": file.filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": existing_dict,
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": existing is not None,
        }

    meter_document_accepted(file_path=stage_path)
    warnings = cross_check_gd_extracted(extracted, shipment_id, db)
    if warnings:
        logger.warning(f"GD cross-check shipment={shipment_id}: {warnings}")

    from modules.weboc.services import _resolve_gd_type_from_data
    extracted["gd_type"] = _resolve_gd_type_from_data(extracted)

    return {
        "staged_file": staged_name,
        "original_filename": file.filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
        "extraction_failed": False,
    }


@router.post("/", response_model=GDSaveResult)
def save_gd(data: GDSave, db: Session = Depends(get_tenant_db),
            current_user: User = Depends(_can_write)):
    if data.shipment_id and data.bl_number:
        shipment = db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first()
        if shipment:
            bl_numbers = [bl.bl_number.strip().upper() for bl in (shipment.bill_of_ladings or []) if bl.bl_number]
            if bl_numbers and data.bl_number.strip().upper() not in bl_numbers:
                raise HTTPException(
                    status_code=400,
                    detail=f"Document Mismatch: GD B/L number '{data.bl_number}' does not match shipment B/L '{bl_numbers[0]}'. Please upload the correct document for this shipment."
                )
    gd = svc.save_gd(data, db, current_user.user_id)
    logger.info(f"GD saved: id={gd.gd_id}, number={gd.gd_number}, status={gd.status}")
    return GDSaveResult(gd_id=gd.gd_id, status=gd.status)


@router.get("/{gd_id}")
def get_gd(gd_id: int, db: Session = Depends(get_tenant_db),
           current_user: User = Depends(get_current_user)):
    return svc.to_response(svc.get_gd_or_404(gd_id, db), db)


@router.put("/{gd_id}", response_model=GDSaveResult)
def update_gd(gd_id: int, data: GDSave, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(_can_write)):
    gd = svc.update_gd(gd_id, data, db, current_user.user_id)
    return GDSaveResult(gd_id=gd_id, status=gd.status)


@router.delete("/{gd_id}")
def delete_gd(gd_id: int, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(_can_write)):
    svc.delete_gd(gd_id, db, deleted_by=current_user.user_id)
    return {"success": True, "gd_id": gd_id}


@router.get("/{gd_id}/document")
def get_document(gd_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    gd = svc.get_gd_or_404(gd_id, db)
    if not gd.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this Goods Declaration")
    return document_file_response(gd.document_path, gd.document_filename)


# ---------------------------------------------------------------------------
# Status workflow  (examination report is OPTIONAL — advancing is never gated)
# ---------------------------------------------------------------------------

@router.post("/{gd_id}/advance")
def advance_status(gd_id: int, request: Request, db: Session = Depends(get_tenant_db),
                   override_reason: str | None = Query(None),
                   current_user: User = Depends(_can_write)):
    """Move the GD to the next stage in its workflow (gd_type-dependent)."""
    gd = svc.get_gd_or_404(gd_id, db)
    if gd.shipment_id:
        check_gate(db, request, gd.shipment_id, ACTION_GD_ADVANCE,
                   user_id=current_user.user_id, override_reason=override_reason, gd_id=gd_id)
    stages = svc.stages_for(gd)
    cur = gd.status if gd.status in stages else "FILED"
    if cur == stages[-1]:
        raise HTTPException(status_code=400, detail=f"GD is already at the final stage ({cur}).")
    nxt = stages[stages.index(cur) + 1]
    if nxt == "CLEARED" and (gd.gd_type or "") == "INTO_BOND":
        from modules.weboc.helpers.weboc_service import bond_summary
        if not bond_summary(gd, db).get("is_weight_settled"):
            raise HTTPException(
                status_code=400,
                detail="Into-Bond GD cannot be cleared until Ex-Bond liftings settle "
                       "gross weight within the 1–2% tolerance.")
    gd.status = nxt
    gd.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"GD {gd_id} advanced {cur} -> {gd.status}")
    return {"success": True, "gd_id": gd_id, "status": gd.status}


@router.put("/{gd_id}/status")
def set_status(gd_id: int, data: GDStatusUpdate, request: Request,
               db: Session = Depends(get_tenant_db),
               current_user: User = Depends(_can_write)):
    """Set a specific status. Must be a stage of the GD's workflow, or DISPUTED (side flag)."""
    gd = svc.get_gd_or_404(gd_id, db)
    target = data.status
    if gd.shipment_id:
        check_gate(db, request, gd.shipment_id, ACTION_GD_SET_STATUS,
                   user_id=current_user.user_id,
                   override_reason=data.override_reason,
                   gd_id=gd_id, target_status=target)
    allowed = set(svc.stages_for(gd)) | {"DISPUTED", "CLEARED"}
    if target not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid status '{target}' for a {gd.gd_type} GD. "
                                   f"Allowed: {sorted(allowed)}")
    if target == "CLEARED" and (gd.gd_type or "") == "INTO_BOND":
        from modules.weboc.helpers.weboc_service import bond_summary
        if not bond_summary(gd, db).get("is_weight_settled"):
            raise HTTPException(
                status_code=400,
                detail="Into-Bond GD cannot be marked Cleared until Ex-Bond liftings settle "
                       "gross weight within the 1–2% tolerance.")
    gd.status = target
    gd.updated_at = datetime.utcnow()
    db.commit()
    return {"success": True, "gd_id": gd_id, "status": gd.status}


@router.post("/{gd_id}/verify")
def verify_gd(gd_id: int, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(_can_write)):
    gd = svc.verify_gd(gd_id, current_user.user_id, db)
    return {"success": True, "gd_id": gd_id, "is_verified": True, "verified_at": gd.verified_at.isoformat() if gd.verified_at else None}


@router.post("/{gd_id}/unverify")
def unverify_gd(gd_id: int, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(_can_write)):
    gd = svc.unverify_gd(gd_id, current_user.user_id, db)
    return {"success": True, "gd_id": gd_id, "is_verified": False}


# ---------------------------------------------------------------------------
# Attachments  (Examination Report / Lab Report images — multiple per GD)
# ---------------------------------------------------------------------------

@router.get("/{gd_id}/attachments")
def list_attachments(gd_id: int, kind: str = Query(None),
                     db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(get_current_user)):
    return [svc.attachment_to_dict(a) for a in svc.list_attachments(gd_id, kind, db)]


@router.post("/{gd_id}/attachments/stage")
async def stage_attachment(
    gd_id: int,
    kind: str = Query(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    from modules.weboc import services as weboc_svc

    svc.get_gd_or_404(gd_id, db)
    kind = kind.upper()
    if kind not in svc.ATTACH_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {svc.ATTACH_KINDS}")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in svc.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")

    contents = await file.read()
    try:
        staged_name, _stage_path = weboc_svc.stage_attachment_bytes(contents, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"staged_file": staged_name, "original_filename": file.filename, "kind": kind}


@router.post("/{gd_id}/attachments")
def upload_attachment(gd_id: int, kind: str = Query(...),
                            file: UploadFile = File(...),
                            db: Session = Depends(get_tenant_db),
                            current_user: User = Depends(_can_write)):
    return svc.add_attachment(gd_id, kind, file, current_user.user_id, db)


@router.get("/attachments/{attachment_id}/file")
def get_attachment_file(attachment_id: int, db: Session = Depends(get_tenant_db),
                        current_user: User = Depends(get_current_user)):
    att = svc.get_attachment_or_404(attachment_id, db)
    if not att.file_path or not os.path.exists(att.file_path):
        raise HTTPException(status_code=404, detail="Attachment not found")
    return document_file_response(att.file_path, att.filename)


@router.delete("/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(_can_write)):
    svc.delete_attachment(attachment_id, db)
    return {"success": True, "attachment_id": attachment_id}
