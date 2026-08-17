"""
Shipment record-keeping documents API — DPL + free-form 'other' supporting documents.
No AI extraction / OCR: stage a file on pick, persist only on explicit save.
Mirrors the shared shipment upload component's file handling (JPG/PNG/PDF).
"""

import os
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tenant import get_tenant_db, get_tenant_context, TenantContext
from core.platform_metering import enforce_document_quota, meter_document_accepted
from config.settings import settings
from models.database_models import ShipmentDocument, Shipment, User
from modules.auth.dependencies import get_current_user
from infrastructure.activity.activity_service import log_activity
from utils.staging import stage_upload, promote_staged, upload_dir as staging_upload_dir
from .shipment_doc_schemas import ShipmentDocSave

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/shipment-docs", tags=["Shipment Documents"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
UPLOAD_SUBDIR = "shipment_documents"
KINDS = ("DPL", "OTHER")


def _to_dict(d: ShipmentDocument) -> dict:
    return {
        "doc_id": d.doc_id,
        "id": d.doc_id,
        "shipment_id": d.shipment_id,
        "doc_kind": d.doc_kind,
        "doc_name": d.doc_name,
        "document_filename": d.document_filename,
        "filename": d.document_filename,
        "name": d.doc_name,
        "has_document": bool(d.document_path and os.path.exists(d.document_path)),
        "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
    }


@router.post("/stage")
def stage_shipment_doc(
    shipment_id: int = Query(...),
    kind: str = Query("OTHER"),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Stage a file without creating or updating a database row."""
    kind = (kind or "OTHER").upper()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {KINDS}")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")

    enforce_document_quota()

    shipment = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    try:
        staged_name, stage_path, _ext = stage_upload(
            file, UPLOAD_SUBDIR, ALLOWED_EXTENSIONS, perm_dir=staging_upload_dir(UPLOAD_SUBDIR),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_doc_id = None
    if kind == "DPL":
        existing = (db.query(ShipmentDocument)
                      .filter(ShipmentDocument.shipment_id == shipment_id,
                              ShipmentDocument.doc_kind == "DPL")
                      .order_by(ShipmentDocument.doc_id.desc()).first())
        if existing:
            existing_doc_id = existing.doc_id

    meter_document_accepted(file_path=stage_path)
    return {
        "staged_file": staged_name,
        "original_filename": file.filename,
        "existing_doc_id": existing_doc_id,
    }


@router.post("/save")
def save_shipment_doc(
    data: ShipmentDocSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Persist a previously staged file on explicit save."""
    kind = (data.kind or "OTHER").upper()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {KINDS}")
    if not data.staged_file:
        raise HTTPException(status_code=400, detail="staged_file is required")

    enforce_document_quota()

    shipment = db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    if kind == "DPL":
        doc_name = (data.name or "").strip() or "DPL Document"
        doc = None
        if data.doc_id:
            doc = db.query(ShipmentDocument).filter(
                ShipmentDocument.doc_id == data.doc_id,
                ShipmentDocument.shipment_id == data.shipment_id,
                ShipmentDocument.doc_kind == "DPL",
            ).first()
        if doc is None:
            doc = (db.query(ShipmentDocument)
                     .filter(ShipmentDocument.shipment_id == data.shipment_id,
                             ShipmentDocument.doc_kind == "DPL")
                     .order_by(ShipmentDocument.doc_id.desc()).first())
    else:
        doc_name = (data.name or "").strip()
        if not doc_name:
            raise HTTPException(status_code=400, detail="A document name is required for other documents.")
        doc = None

    replacing = doc is not None
    if doc is None:
        doc = ShipmentDocument(
            shipment_id=data.shipment_id,
            doc_kind=kind,
            doc_name=doc_name,
            uploaded_by=current_user.user_id,
        )
        db.add(doc)
        db.flush()
    else:
        doc.doc_name = doc_name
        doc.updated_by = current_user.user_id
        doc.updated_at = datetime.utcnow()

    orig = data.original_filename or "document.pdf"
    dest, promoted_name = promote_staged(
        UPLOAD_SUBDIR, data.staged_file, doc.doc_id, orig, ALLOWED_EXTENSIONS,
        perm_dir=staging_upload_dir(UPLOAD_SUBDIR),
    )
    if not dest:
        raise HTTPException(status_code=400, detail="Staged document not found — please re-upload the file.")

    old_path = doc.document_path
    doc.document_path = dest
    doc.document_filename = promoted_name or orig
    log_activity(db, data.shipment_id, current_user.user_id,
                 "REPLACE" if replacing else "UPLOAD", doc_type=doc_name)
    db.commit()
    db.refresh(doc)

    if old_path and old_path != dest and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass

    meter_document_accepted(file_path=doc.document_path)
    return _to_dict(doc)


@router.get("/by-shipment/{shipment_id}")
def docs_for_shipment(shipment_id: int, db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(get_current_user)):
    rows = (db.query(ShipmentDocument)
              .filter(ShipmentDocument.shipment_id == shipment_id)
              .order_by(ShipmentDocument.doc_id.asc()).all())
    return {"shipment_id": shipment_id, "count": len(rows),
            "items": [_to_dict(r) for r in rows]}


@router.get("/{doc_id}/document")
def get_document(doc_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    doc = db.query(ShipmentDocument).filter(ShipmentDocument.doc_id == doc_id).first()
    if not doc or not doc.document_path:
        raise HTTPException(status_code=404, detail="No document attached to this entry")
    return document_file_response(doc.document_path, doc.document_filename)


@router.delete("/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(get_current_user)):
    doc = db.query(ShipmentDocument).filter(ShipmentDocument.doc_id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.document_path and os.path.exists(doc.document_path):
        try:
            os.remove(doc.document_path)
        except OSError:
            pass
    log_activity(db, doc.shipment_id, current_user.user_id, "DELETE", doc_type=doc.doc_name)
    db.delete(doc)
    db.commit()
    return {"success": True, "doc_id": doc_id}
