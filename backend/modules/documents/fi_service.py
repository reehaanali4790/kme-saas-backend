"""Business logic for Financial Instruments, extracted from modules/documents/fi_router.py
as part of the Phase 4 module rollout.
"""
import os
import shutil
from datetime import datetime, date

from fastapi import UploadFile
from sqlalchemy.orm import Session

from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import FinancialInstrument, Shipment
from modules.documents.fi_schemas import FISave, STR_FIELDS, DATE_FIELDS, DEC_FIELDS
from utils.uploads import safe_upload_path

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "fi_documents")


def save_file(upload: UploadFile, fi_id: int) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = safe_upload_path(UPLOAD_DIR, fi_id, upload.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def apply_fi_fields(fi: FinancialInstrument, data: FISave, user_id: int):
    fields_set = data.model_fields_set

    for f in STR_FIELDS:
        v = getattr(data, f)
        if f in fields_set and v is not None:
            setattr(fi, f, str(v).strip() or None)

    for f in DEC_FIELDS:
        if f in fields_set:
            setattr(fi, f, getattr(data, f))

    for f in DATE_FIELDS:
        v = getattr(data, f)
        if v:
            setattr(fi, f, v)

    if "lc_id" in fields_set:
        fi.lc_id = data.lc_id or None
    if "shipment_id" in fields_set:
        fi.shipment_id = data.shipment_id or None
    if data.status:
        fi.status = data.status

    fi.updated_by = user_id
    fi.updated_at = datetime.utcnow()


def to_dict(fi: FinancialInstrument) -> dict:
    def n(v):
        return float(v) if v is not None else None

    out = {
        "fi_id": fi.fi_id, "shipment_id": fi.shipment_id, "lc_id": fi.lc_id,
        "document_filename": fi.document_filename,
        "has_document": bool(fi.document_path and os.path.exists(fi.document_path)),
        "status": fi.status,
    }
    for f in STR_FIELDS:
        out[f] = getattr(fi, f)
    for f in DATE_FIELDS:
        v = getattr(fi, f)
        out[f] = v.isoformat() if v else None
    for f in DEC_FIELDS:
        out[f] = n(getattr(fi, f))
    return out


def get_fi_or_404(db: Session, fi_id: int) -> FinancialInstrument:
    fi = db.query(FinancialInstrument).filter(FinancialInstrument.fi_id == fi_id).first()
    if not fi:
        raise NotFoundError("FI not found")
    return fi


def save_fi(db: Session, data: FISave, user_id: int) -> FinancialInstrument:
    if data.fi_id:
        fi = db.query(FinancialInstrument).filter(FinancialInstrument.fi_id == data.fi_id).first()
        if not fi:
            raise NotFoundError("FI placeholder not found")
    else:
        shipment = (db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first()
                    if data.shipment_id else None)
        fi = FinancialInstrument(shipment_id=data.shipment_id,
                                 lc_id=shipment.lc_id if shipment else None,
                                 source="MANUAL", status="PENDING_REVIEW",
                                 created_by=user_id)
        db.add(fi)
        db.flush()

    apply_fi_fields(fi, data, user_id)
    if not fi.status or fi.status == "PENDING_REVIEW":
        fi.status = "VERIFIED"
    return fi


def update_fi(db: Session, fi_id: int, data: FISave, user_id: int) -> FinancialInstrument:
    fi = get_fi_or_404(db, fi_id)
    apply_fi_fields(fi, data, user_id)
    return fi


def delete_fi(db: Session, fi_id: int) -> FinancialInstrument:
    fi = get_fi_or_404(db, fi_id)
    if fi.document_path and os.path.exists(fi.document_path):
        try:
            os.remove(fi.document_path)
        except OSError:
            pass
    db.delete(fi)
    return fi


def check_expiry_warning(extracted: dict) -> list:
    warnings = []
    exp_raw = extracted.get("expiry_date")
    if exp_raw:
        try:
            exp = date.fromisoformat(str(exp_raw)[:10])
            if exp < date.today():
                warnings.append(
                    f"This FI expired on {exp.isoformat()} ({(date.today() - exp).days} "
                    f"day(s) ago). You can still save it, but the filing window has passed.")
        except ValueError:
            pass
    return warnings
