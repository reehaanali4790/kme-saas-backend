"""Business logic for Insurance Certificates, extracted from
modules/documents/insurance_router.py as part of the Phase 4 module rollout.

modules/shipments/services.py imports `verify_one` from here (it used to import
`_verify_one` from insurance_router.py directly) - keep that name stable.
"""
import os
import re
import shutil
from datetime import datetime
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import InsuranceCertificate, Shipment
from modules.documents.insurance_schemas import InsuranceSave, STR_FIELDS, DATE_FIELDS, DEC_FIELDS
from utils.uploads import safe_upload_path

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "insurance_documents")


def save_file(upload: UploadFile, insurance_id: int) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = safe_upload_path(UPLOAD_DIR, insurance_id, upload.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def apply_insurance_fields(ins: InsuranceCertificate, data: InsuranceSave, user_id: int):
    fields_set = data.model_fields_set

    for f in STR_FIELDS:
        v = getattr(data, f)
        if f in fields_set and v is not None:
            setattr(ins, f, str(v).strip() or None)

    for f in DEC_FIELDS:
        if f in fields_set:
            setattr(ins, f, getattr(data, f))

    for f in DATE_FIELDS:
        v = getattr(data, f)
        if v:
            setattr(ins, f, v)

    if "lc_id" in fields_set:
        ins.lc_id = data.lc_id or None
    if "shipment_id" in fields_set:
        ins.shipment_id = data.shipment_id or None
    if data.status:
        ins.status = data.status

    ins.updated_by = user_id
    ins.updated_at = datetime.utcnow()


def norm(v):
    """Normalise a reference number for tolerant comparison (case/space/punct-insensitive)."""
    if v is None:
        return None
    s = re.sub(r"[\s\-_/.]", "", str(v)).upper()
    return s or None


def verify_one(extracted_val, shipment_val):
    """MATCHED / NOT_MATCHED / NOT_FOUND for a single reference number."""
    e = norm(extracted_val)
    if not e:
        return "NOT_FOUND"
    s = norm(shipment_val)
    if not s:
        return "NOT_FOUND"
    return "MATCHED" if e == s else "NOT_MATCHED"


def shipment_bl_lc(shipment: Shipment):
    """The shipment's authoritative BL number and LC number for cross-checking."""
    bl = shipment.bill_of_ladings[0] if shipment and shipment.bill_of_ladings else None
    bl_number = bl.bl_number if bl else None
    lc_number = shipment.lc.lc_number if (shipment and shipment.lc) else None
    return bl_number, lc_number


def verify(bl_value, lc_value, shipment: Shipment) -> dict:
    bl_number, lc_number = shipment_bl_lc(shipment)
    return {
        "bl": verify_one(bl_value, bl_number),
        "lc": verify_one(lc_value, lc_number),
        "shipment_bl_number": bl_number,
        "shipment_lc_number": lc_number,
    }


def to_dict(ins: InsuranceCertificate, shipment: Shipment | None = None) -> dict:
    def n(v):
        return float(v) if v is not None else None

    out: dict[str, Any] = {
        "insurance_id": ins.insurance_id, "shipment_id": ins.shipment_id, "lc_id": ins.lc_id,
        "document_filename": ins.document_filename,
        "has_document": bool(ins.document_path and os.path.exists(ins.document_path)),
        "status": ins.status,
    }
    for f in STR_FIELDS:
        out[f] = getattr(ins, f)
    for f in DATE_FIELDS:
        v = getattr(ins, f)
        out[f] = v.isoformat() if v else None
    for f in DEC_FIELDS:
        out[f] = n(getattr(ins, f))
    # Premium Rate % = gross premium / sum insured * 100 (used to verify the contracted
    # insurance rate, normally ~0.25%). Null unless both numbers are present & positive.
    gp, si = n(ins.gross_premium), n(ins.sum_insured)
    out["premium_rate_pct"] = round(gp / si * 100, 4) if (gp and si and si > 0) else None
    if shipment is not None:
        out["verification"] = verify(ins.bl_number, ins.lc_number, shipment)
    return out


def get_insurance_or_404(db: Session, insurance_id: int) -> InsuranceCertificate:
    ins = db.query(InsuranceCertificate).filter(
        InsuranceCertificate.insurance_id == insurance_id).first()
    if not ins:
        raise NotFoundError("Insurance certificate not found")
    return ins


def save_insurance(db: Session, data: InsuranceSave, user_id: int) -> InsuranceCertificate:
    if data.insurance_id:
        ins = get_insurance_or_404(db, data.insurance_id)
    else:
        shipment = (db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first()
                    if data.shipment_id else None)
        ins = InsuranceCertificate(shipment_id=data.shipment_id,
                                   lc_id=shipment.lc_id if shipment else None,
                                   source="MANUAL", status="PENDING_REVIEW",
                                   created_by=user_id)
        db.add(ins)
        db.flush()

    apply_insurance_fields(ins, data, user_id)
    if not ins.status or ins.status == "PENDING_REVIEW":
        ins.status = "VERIFIED"
    return ins


def update_insurance(db: Session, insurance_id: int, data: InsuranceSave,
                     user_id: int) -> InsuranceCertificate:
    ins = get_insurance_or_404(db, insurance_id)
    apply_insurance_fields(ins, data, user_id)
    return ins


def delete_insurance(db: Session, insurance_id: int) -> InsuranceCertificate:
    ins = get_insurance_or_404(db, insurance_id)
    if ins.document_path and os.path.exists(ins.document_path):
        try:
            os.remove(ins.document_path)
        except OSError:
            pass
    db.delete(ins)
    return ins
