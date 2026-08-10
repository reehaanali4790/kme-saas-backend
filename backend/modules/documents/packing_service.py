"""Business logic for Packing Lists, extracted from modules/documents/packing_router.py
as part of the Phase 4 module rollout.
"""
import os
import shutil
from datetime import datetime
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session, joinedload

from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import PackingList, PackingLineItem, Shipment
from modules.documents.packing_schemas import PackingSave, STR_FIELDS, DEC_FIELDS
from utils.uploads import safe_upload_path

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "packing_documents")


def save_file(upload: UploadFile, packing_id: int) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = safe_upload_path(UPLOAD_DIR, packing_id, upload.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def apply_fields(p: PackingList, data: PackingSave, user_id: int):
    fields_set = data.model_fields_set

    for f in STR_FIELDS:
        v = getattr(data, f)
        if f in fields_set and v is not None:
            setattr(p, f, str(v).strip() or None)

    for f in DEC_FIELDS:
        if f in fields_set:
            setattr(p, f, getattr(data, f))

    if "total_coils" in fields_set:
        p.total_coils = data.total_coils

    if data.packing_date:
        p.packing_date = data.packing_date

    if "upload_date" in fields_set:
        p.upload_date = data.upload_date

    p.updated_by = user_id
    p.updated_at = datetime.utcnow()


def replace_line_items(p: PackingList, items: Optional[list], db: Session):
    db.query(PackingLineItem).filter(PackingLineItem.packing_id == p.packing_id).delete()
    for it in (items or []):
        db.add(PackingLineItem(
            packing_id=p.packing_id, shipment_id=p.shipment_id,
            item_number=it.item_number,
            grade=it.grade,
            size=it.size,
            size_thickness_mm=it.size_thickness_mm,
            size_width_mm=it.size_width_mm,
            quantity_mt=it.quantity_mt,
            net_weight_mt=it.net_weight_mt,
            gross_weight_mt=it.gross_weight_mt,
            number_of_coils=it.number_of_coils,
        ))


def to_dict(p: PackingList) -> dict:
    return {
        "packing_id": p.packing_id, "shipment_id": p.shipment_id,
        "document_filename": p.document_filename,
        "has_document": bool(p.document_path and os.path.exists(p.document_path)),
        "packing_number": p.packing_number,
        "packing_date": p.packing_date.isoformat() if p.packing_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": p.upload_date.isoformat() if p.upload_date else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "goods_description": p.goods_description, "hs_code": p.hs_code,
        "total_net_weight_mt": float(p.total_net_weight_mt) if p.total_net_weight_mt else None,
        "total_gross_weight_mt": float(p.total_gross_weight_mt) if p.total_gross_weight_mt else None,
        "total_coils": p.total_coils, "status": p.status,
        "line_items": [{
            "item_number": li.item_number,
            "grade": li.grade, "size": li.size,
            "size_thickness_mm": float(li.size_thickness_mm) if li.size_thickness_mm else None,
            "size_width_mm": float(li.size_width_mm) if li.size_width_mm else None,
            "quantity_mt": float(li.quantity_mt) if li.quantity_mt else None,
            "net_weight_mt": float(li.net_weight_mt) if li.net_weight_mt else None,
            "gross_weight_mt": float(li.gross_weight_mt) if li.gross_weight_mt else None,
            "number_of_coils": li.number_of_coils,
        } for li in sorted(p.line_items, key=lambda x: x.item_number or 0)],
    }


def get_packing_or_404(db: Session, packing_id: int, with_items: bool = False) -> PackingList:
    q = db.query(PackingList)
    if with_items:
        q = q.options(joinedload(PackingList.line_items))
    p = q.filter(PackingList.packing_id == packing_id).first()
    if not p:
        raise NotFoundError("Packing list not found")
    return p


def save_packing(db: Session, data: PackingSave, user_id: int) -> PackingList:
    if data.packing_id:
        p = db.query(PackingList).filter(PackingList.packing_id == data.packing_id).first()
        if not p:
            raise NotFoundError("Packing placeholder not found")
    else:
        p = PackingList(shipment_id=data.shipment_id, source="MANUAL", created_by=user_id)
        db.add(p)
        db.flush()

    apply_fields(p, data, user_id)
    p.status = "VERIFIED"
    db.flush()
    replace_line_items(p, data.line_items, db)

    # mark shipment progressing
    if p.shipment_id:
        s = db.query(Shipment).filter(Shipment.shipment_id == p.shipment_id).first()
        if s and s.status == "PENDING":
            s.status = "DOCUMENTS_RECEIVED"
    return p


def update_packing(db: Session, packing_id: int, data: PackingSave, user_id: int) -> PackingList:
    p = get_packing_or_404(db, packing_id)
    apply_fields(p, data, user_id)
    db.flush()
    if "line_items" in data.model_fields_set:
        replace_line_items(p, data.line_items, db)
    return p


def delete_packing(db: Session, packing_id: int) -> PackingList:
    p = get_packing_or_404(db, packing_id)
    if p.document_path and os.path.exists(p.document_path):
        try:
            os.remove(p.document_path)
        except OSError:
            pass
    db.delete(p)
    return p
