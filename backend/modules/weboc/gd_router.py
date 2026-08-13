"""
Goods Declaration API — upload, AI extract, verify/save, CRUD. Record-only.
GD attaches to a shipment.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
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

    # One GD per shipment — reuse the existing record instead of piling up
    # duplicate / abandoned rows when a document is (re-)uploaded.
    gd = (db.query(GoodsDeclaration)
            .filter(GoodsDeclaration.shipment_id == shipment_id)
            .order_by(GoodsDeclaration.gd_id.desc()).first())
    is_replace = gd is not None
    if gd is None:
        gd = GoodsDeclaration(shipment_id=shipment_id, lc_id=shipment.lc_id,
                              source="UPLOADED", status="FILED", created_by=current_user.user_id)
        db.add(gd)
        db.flush()

    # Keep the old document + the GD's existing columns/items until the new document has
    # actually been extracted, so a failed extraction can't wipe a filed GD.
    old_path = gd.document_path
    gd.document_path = svc.save_file(file, gd.gd_id)
    gd.document_filename = file.filename
    db.commit()
    meter_document_accepted(file_path=gd.document_path)

    extracted, extraction_error = safe_extract(
        extract_gd, gd.document_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Goods Declaration, gd_id={gd.gd_id}, file={file.filename}")

    if extraction_error:
        db.commit()
        logger.warning(f"GD {gd.gd_id}: extraction failed, falling back to manual entry "
                       f"(previous data preserved, replace={is_replace}).")
        return {"gd_id": gd.gd_id, "document_filename": gd.document_filename,
                "is_pdf": ext == ".pdf",
                "extracted": svc.to_response(gd) if is_replace else {}, "warnings": [],
                "extraction_failed": True, "extraction_message": extraction_error,
                "had_previous_data": is_replace}

    if old_path and old_path != gd.document_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    gd.source = "UPLOADED"
    gd.raw_extracted_data = extracted
    # Populate the GD's columns from the extraction immediately, so the GD number and
    # all parsed fields show on the Customs tab / GD detail even before the user runs
    # the verify-save step. The later verify-save overwrites with confirmed values.
    extracted_data = GDSave(**{k: v for k, v in extracted.items() if k in GDSave.model_fields})
    svc.apply_gd_fields(gd, extracted_data)
    svc.normalize_gd(gd, db)
    svc.replace_items(gd, extracted_data.items, db)
    db.commit()

    # Cross-check the GD's LC# / BL# against the shipment's other documents so the user
    # is warned BEFORE saving if this GD looks like it belongs to a different LC/shipment.
    warnings = cross_check_gd_extracted(extracted, shipment_id, db)
    if warnings:
        logger.warning(f"GD cross-check gd_id={gd.gd_id}: {warnings}")

    # Surface the resolved gd_type (from declaration type / prefix) so the verify form
    # can pre-select the GD-type dropdown.
    extracted["gd_type"] = gd.gd_type

    return {"gd_id": gd.gd_id, "document_filename": gd.document_filename,
            "is_pdf": ext == ".pdf", "extracted": extracted, "warnings": warnings,
            "extraction_failed": False}


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
def advance_status(gd_id: int, db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(_can_write)):
    """Move the GD to the next stage in its workflow (gd_type-dependent)."""
    gd = svc.get_gd_or_404(gd_id, db)
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
def set_status(gd_id: int, data: GDStatusUpdate, db: Session = Depends(get_tenant_db),
               current_user: User = Depends(_can_write)):
    """Set a specific status. Must be a stage of the GD's workflow, or DISPUTED (side flag)."""
    gd = svc.get_gd_or_404(gd_id, db)
    target = data.status
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
