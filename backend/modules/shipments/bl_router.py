"""
Bill of Lading API endpoints — CRUD, upload, Claude Vision extraction.
"""

import os
import shutil
import uuid
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from infrastructure.documents.document_files import document_file_response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core.tenant import get_tenant_db, TenantContext, get_tenant_context
from config.settings import settings
from models.database_models import BillOfLading, LCMaster, Shipment, User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from modules.shipments.extractors.bl_extractor import extract_bl_from_image
from infrastructure.document_ai.document_ai import ExtractionError
from modules.shipments import bl_service as svc
from modules.shipments.bl_schemas import BLLinkLC, BLSave, BLStatusUpdate
from modules.shipments.container_detention_service import resolve_bl_type
from utils.uploads import safe_upload_path, tenant_upload_dir

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/bl", tags=["Bill of Lading"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def _bl_upload_dir(tenant: TenantContext) -> str:
    return tenant_upload_dir(settings.UPLOAD_DIR, tenant.schema_name, "bl_documents")

# Mutations require OPERATOR+ - VIEWER can read BLs but not create/edit/delete them.
_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


def _safe_remove(path) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GET /api/bl/  — list all BLs
# ---------------------------------------------------------------------------

@router.get("/")
def list_bls(
    status: Optional[str] = Query(None),
    lc_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(BillOfLading).options(joinedload(BillOfLading.lc))

    if status:
        q = q.filter(BillOfLading.status == status.upper())
    if lc_id:
        q = q.filter(BillOfLading.lc_id == lc_id)
    if search:
        term = f"%{search.upper()}%"
        q = q.filter(
            BillOfLading.bl_number.ilike(term) |
            BillOfLading.vessel_name.ilike(term) |
            BillOfLading.port_of_loading.ilike(term) |
            BillOfLading.port_of_discharge.ilike(term) |
            BillOfLading.shipper_name.ilike(term)
        )

    total = q.count()
    bls = q.order_by(BillOfLading.created_at.desc()) \
           .offset((page - 1) * page_size) \
           .limit(page_size) \
           .all()

    config = svc.get_demurrage_config(db)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [svc.bl_to_dict(b, config, db) for b in bls],
    }


# ---------------------------------------------------------------------------
# GET /api/bl/by-lc/{lc_id}  — BLs for a specific LC
# ---------------------------------------------------------------------------

@router.get("/by-lc/{lc_id}")
def get_bls_for_lc(
    lc_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    bls = db.query(BillOfLading) \
            .options(joinedload(BillOfLading.lc)) \
            .filter(BillOfLading.lc_id == lc_id) \
            .order_by(BillOfLading.bl_date.desc()) \
            .all()
    config = svc.get_demurrage_config(db)
    return {"lc_id": lc_id, "count": len(bls), "items": [svc.bl_to_dict(b, config, db) for b in bls]}


# ---------------------------------------------------------------------------
# GET /api/bl/{bl_id}  — single BL
# ---------------------------------------------------------------------------

@router.get("/{bl_id}")
def get_bl(
    bl_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    bl = db.query(BillOfLading) \
           .options(joinedload(BillOfLading.lc)) \
           .filter(BillOfLading.bl_id == bl_id) \
           .first()
    if not bl:
        raise HTTPException(status_code=404, detail="BL not found")
    return svc.bl_to_dict(bl, svc.get_demurrage_config(db), db)


# ---------------------------------------------------------------------------
# GET /api/bl/{bl_id}/document  — serve stored file
# ---------------------------------------------------------------------------

@router.get("/{bl_id}/document")
def get_bl_document(
    bl_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    bl = db.query(BillOfLading).filter(BillOfLading.bl_id == bl_id).first()
    if not bl or not bl.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this BL")
    return document_file_response(bl.document_path, bl.document_filename)


# ---------------------------------------------------------------------------
# POST /api/bl/upload-and-extract  — upload image, run Claude, return JSON
# ---------------------------------------------------------------------------

@router.post("/upload-and-extract")
def upload_and_extract(
    file: UploadFile = File(...),
    shipment_id: Optional[int] = Query(None),
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
    current_user: User = Depends(_can_write),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only JPG, PNG and PDF files are supported. Got: {ext}"
        )

    if not settings.ANTHROPIC_API_KEY and not settings.GEMINI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI extraction is not set up on this server. Please contact support, "
                   "or enter the details manually."
        )

    from core.platform_metering import enforce_document_quota, meter_document_accepted

    # Quota check only — count the doc after the file is actually accepted.
    enforce_document_quota(tenant.organization_id)

    # If created within a shipment, inherit the shipment + its LC
    shipment_lc_id = None
    if shipment_id:
        sh = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
        if not sh:
            raise HTTPException(status_code=404, detail="Shipment not found")
        shipment_lc_id = sh.lc_id

    # Save to a TEMP file and verify it's actually a Bill of Lading BEFORE touching the
    # shipment's existing BL — so a wrong document (GD, IGM, invoice, ...) is rejected
    # without overwriting good data.
    bl_dir = _bl_upload_dir(tenant)
    os.makedirs(bl_dir, exist_ok=True)
    tmp_path = os.path.join(bl_dir, f"_tmp_{uuid.uuid4().hex}{ext}")
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        extracted = extract_bl_from_image(tmp_path, settings.ANTHROPIC_API_KEY)
    except ExtractionError as e:
        _safe_remove(tmp_path)
        logger.error(f"BL extraction failed for {file.filename}: {e.detail}")
        raise HTTPException(status_code=422, detail=e.user_message)
    except Exception as e:
        _safe_remove(tmp_path)
        logger.error(f"BL extraction failed for {file.filename}: {type(e).__name__}: {e}",
                     exc_info=True)
        raise HTTPException(
            status_code=422,
            detail="Could not read this Bill of Lading automatically. Please try a clearer "
                   "file, or enter the BL details manually.")

    # Wrong-document guard: reject anything that isn't a Bill of Lading.
    if extracted.get("is_bill_of_lading") is False:
        _safe_remove(tmp_path)
        detected = extracted.get("document_type") or "a different document"
        raise HTTPException(
            status_code=422,
            detail=f"This file does not look like a Bill of Lading — it appears to be {detected}. "
                   f"Please upload a Bill of Lading.")

    # Valid BL — pick the record this document belongs to and move the file in.
    # Reuse priority (so the same BL number never spawns a parallel row that would
    # later collide on the duplicate-number guard):
    #   1) An existing BL with the SAME number — re-uploading a known BL: overwrite
    #      it (and relink it to this shipment) instead of creating a duplicate.
    #   2) The shipment's existing BL placeholder.
    #   3) A brand-new row.
    incoming_number = (extracted.get("bl_number") or "").strip()

    bl = None
    if incoming_number:
        bl = (db.query(BillOfLading)
                .filter(func.upper(BillOfLading.bl_number) == incoming_number.upper())
                .order_by(BillOfLading.bl_id.desc()).first())

    shipment_bl = None
    if shipment_id:
        shipment_bl = (db.query(BillOfLading)
                .filter(BillOfLading.shipment_id == shipment_id)
                .order_by(BillOfLading.bl_id.desc()).first())
    if bl is None:
        bl = shipment_bl

    if bl is None:
        bl = BillOfLading(
            source="UPLOADED", status="PENDING_REVIEW",
            shipment_id=shipment_id, lc_id=shipment_lc_id,
            created_by=current_user.user_id,
        )
        db.add(bl)
        db.flush()
    else:
        _safe_remove(bl.document_path)
        bl.status = "PENDING_REVIEW"
        bl.source = "UPLOADED"
        bl.updated_by = current_user.user_id
        # Relink to the shipment we're uploading under (handles re-uploading a BL
        # that previously lived standalone or on another shipment).
        if shipment_id:
            bl.shipment_id = shipment_id
            if shipment_lc_id and not bl.lc_id:
                bl.lc_id = shipment_lc_id
        # If we matched by number but the shipment also had a separate *empty*
        # placeholder, drop it so the shipment isn't left with a dangling duplicate.
        if shipment_bl is not None and shipment_bl.bl_id != bl.bl_id \
                and not (shipment_bl.bl_number or "").strip():
            _safe_remove(shipment_bl.document_path)
            db.delete(shipment_bl)
        db.flush()

    stored_path = safe_upload_path(bl_dir, bl.bl_id, file.filename, ALLOWED_EXTENSIONS)
    os.replace(tmp_path, stored_path)
    bl.document_filename = file.filename
    bl.document_path = stored_path
    bl.raw_extracted_data = extracted
    bl.bl_type = resolve_bl_type(extracted, bl, db)
    extracted["bl_type"] = bl.bl_type
    db.commit()
    db.refresh(bl)

    # Real metering: document accepted + storage. AI event already recorded by extract_with_tiers.
    meter_document_accepted(tenant.organization_id, file_path=stored_path)

    return {
        "bl_id": bl.bl_id,
        "document_filename": bl.document_filename,
        "extracted": extracted,
        "is_pdf": ext == ".pdf",
    }


# ---------------------------------------------------------------------------
# POST /api/bl/  — save verified BL (create)
# ---------------------------------------------------------------------------

from modules.lc_creation.helpers.shipment_validator import validate_shipment

@router.post("/")
def create_bl(
    data: BLSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    bl = svc.create_bl(data, db, current_user.user_id)
    if bl.shipment_id:
        validate_shipment(bl.shipment_id, db)
    logger.info(f"BL created: bl_id={bl.bl_id}, bl_number={bl.bl_number}, by={current_user.username}")
    return {"success": True, "bl_id": bl.bl_id, "bl_number": bl.bl_number}


# ---------------------------------------------------------------------------
# PUT /api/bl/{bl_id}  — update BL fields
# ---------------------------------------------------------------------------

@router.put("/{bl_id}")
def update_bl(
    bl_id: int,
    data: BLSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    bl = svc.update_bl(bl_id, data, db, current_user.user_id)
    if bl.shipment_id:
        validate_shipment(bl.shipment_id, db)
    return {"success": True, "bl_id": bl.bl_id}


# ---------------------------------------------------------------------------
# PUT /api/bl/{bl_id}/link-lc  — tag BL to an LC
# ---------------------------------------------------------------------------

@router.put("/{bl_id}/link-lc")
def link_bl_to_lc(
    bl_id: int,
    data: BLLinkLC,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    lc = svc.link_bl_to_lc(bl_id, data.lc_id, db, current_user.user_id)
    return {"success": True, "bl_id": bl_id, "lc_id": data.lc_id, "lc_number": lc.lc_number}


# ---------------------------------------------------------------------------
# PUT /api/bl/{bl_id}/status  — change status
# ---------------------------------------------------------------------------

@router.put("/{bl_id}/status")
def update_bl_status(
    bl_id: int,
    data: BLStatusUpdate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    bl = svc.update_bl_status(bl_id, data.status, data.notes, db, current_user.user_id)
    return {"success": True, "bl_id": bl_id, "status": bl.status}


# ---------------------------------------------------------------------------
# DELETE /api/bl/{bl_id}
# ---------------------------------------------------------------------------

@router.delete("/{bl_id}")
def delete_bl(
    bl_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    svc.delete_bl(bl_id, db)
    logger.info(f"BL deleted: bl_id={bl_id}, by={current_user.username}")
    return {"success": True, "bl_id": bl_id}


# ---------------------------------------------------------------------------
# LC search helper — used by upload wizard step 3
# ---------------------------------------------------------------------------

@router.get("/search/lcs")
def search_lcs(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Search LCs by LC number or supplier for the link-to-LC step."""
    term = f"%{q.upper()}%"
    lcs = db.query(LCMaster) \
            .filter(
                (LCMaster.lc_number.ilike(term)) |
                (LCMaster.supplier_name.ilike(term))
            ) \
            .order_by(LCMaster.lc_date.desc()) \
            .limit(15) \
            .all()
    return [
        {
            "lc_id": lc.lc_id,
            "lc_number": lc.lc_number,
            "supplier_name": lc.supplier_name,
            "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
            "status": lc.status,
            "bl_count": len(lc.bill_of_ladings),
        }
        for lc in lcs
    ]
