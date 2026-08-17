"""Base Goods Declaration CRUD business logic - status workflow, item lines,
attachments, party-name normalization. Extracted from modules/weboc/gd_router.py.

NOTE: modules/weboc/services.py imports `recompute_gd_status` and `ATTACH_DIR` from
here (it used to import them from gd_router.py directly) - keep those names stable.
"""
import os
import re
import shutil
from datetime import datetime
from typing import Any, List, Optional, Tuple, cast

from fastapi import UploadFile
from sqlalchemy.orm import Session

from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import GDAttachment, GDItem, GoodsDeclaration, Shipment
from modules.weboc.gd_schemas import GDItemIn, GDSave, LevyIn
from infrastructure.normalization.normalization_service import normalize_goods_declaration
from utils.uploads import safe_upload_path
from utils.staging import promote_staged, replace_document_path
from core.platform_metering import enforce_document_quota, meter_document_accepted

UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "gd_documents")
ATTACH_DIR = os.path.join(settings.UPLOAD_DIR, "gd_attachments")
STAGE_SUBDIR = "gd_documents"
ATTACH_STAGE_SUBDIR = "gd_attachments"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def _gd_upload_dir() -> str:
    from utils.staging import upload_dir
    return upload_dir(STAGE_SUBDIR)


def _attach_upload_dir() -> str:
    from utils.staging import upload_dir
    return upload_dir(ATTACH_STAGE_SUBDIR)


def attach_upload_dir() -> str:
    """Tenant-scoped GD attachment directory (respects request tenant context)."""
    return _attach_upload_dir()

GD_TYPES = ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND")
ATTACH_KINDS = ("EXAMINATION", "LAB", "ASSESSMENT", "GD_VIEW", "ITEM_DETAILS", "FINAL_GD",
                "INTO_BOND_GD", "EX_BOND_GD")
DECL_TYPE_MAP = {"IB": "INTO_BOND", "HC": "HOME_CONSUMPTION", "EX": "EX_BOND"}

# Stage order per GD type.
# Into-bond ends at CLEARED only after gross weight is settled via Ex-Bond liftings
# (within 1-2% tolerance). HC / Ex-Bond end at RELEASED.
# DISPUTED is a side flag reachable from anywhere (not part of the linear order).
_BASE_STAGES = ["DRAFT", "FILED", "EXAMINATION", "ASSESSED"]
WORKFLOW = {
    "HOME_CONSUMPTION": _BASE_STAGES + ["RELEASED"],
    "EX_BOND":          _BASE_STAGES + ["RELEASED"],
    "INTO_BOND":        _BASE_STAGES + ["INTO_BOND", "CLEARED"],
}

_STR_FIELDS = ["gd_number", "machine_number", "custom_office", "bank_code", "girm_egm_number",
               "financial_instrument_no", "importer_ntn", "importer_name", "exporter_name",
               "vessel_name", "bl_number", "port_of_shipment", "port_of_discharge",
               "payment_terms", "hs_code", "goods_description", "package_type",
               "country_of_origin", "notes", "examination_report", "vir_no", "index_no",
               "lab_officer", "lab_report", "assessment_report"]
_DATE_FIELDS = ["filing_date", "assessment_date", "financial_instrument_date", "bl_date",
                "examined_date"]
_DEC_FIELDS = ["financial_instrument_value", "gross_weight_mt", "net_weight_mt", "bonded_qty_mt",
               "declared_unit_value_usd", "assessed_unit_value_usd", "declared_value_usd",
               "assessed_value_usd", "declared_value_pkr", "assessed_value_pkr",
               "exchange_rate_pkr", "freight_value_usd", "insurance_value_usd",
               "landing_charges_pct", "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
               "additional_customs_duty_pkr", "additional_sales_tax_pkr", "regulatory_duty_pkr",
               "igm_deblocking_pkr", "extra_pkr", "invoice_not_found_penalty_pkr",
               "total_duties_pkr"]
_INT_FIELDS = ["package_count"]
_BOOL_FIELDS = ["lab_report_available", "lab_report_assessment"]


def stages_for(gd: GoodsDeclaration) -> list:
    gd_type = cast(Optional[str], gd.gd_type)
    return WORKFLOW.get(gd_type or "HOME_CONSUMPTION", WORKFLOW["HOME_CONSUMPTION"])


def gd_is_closed(gd: GoodsDeclaration) -> bool:
    """True once the GD has reached the end of its workflow - RELEASED for HC / Ex-Bond,
    CLEARED for Into-Bond. This is the point the cargo is trucked out and the KGTL
    weighbridge reconciliation applies. DISPUTED is not a terminal stage, so it is False."""
    stages = stages_for(gd)
    return gd.status == stages[-1]


def _has_attachment(gd_id: int, kind: str, db: Session) -> bool:
    return db.query(GDAttachment.attachment_id).filter(
        GDAttachment.gd_id == gd_id, GDAttachment.kind == kind).first() is not None


def examination_report_available(gd: GoodsDeclaration, db: Session) -> bool:
    """True when the customs examination report section has meaningful data."""
    gd_id = cast(int, gd.gd_id)
    if gd.examination_report and str(gd.examination_report).strip():
        return True
    if _has_attachment(gd_id, "EXAMINATION", db):
        return True
    if not gd.examined_date:
        return False
    return bool(
        (gd.vir_no and str(gd.vir_no).strip())
        or (gd.index_no and str(gd.index_no).strip())
    )


def recompute_gd_status(gd: GoodsDeclaration, db: Session) -> str:
    """Derive the GD / WebOC status automatically from completed steps (monotonic - only
    ever advances, never demotes). Order:
        FILED       <- View GD uploaded (GD_VIEW) or GD data saved (gd_number/document)
        EXAMINATION <- Examination report text or an EXAMINATION image added
        ASSESSED    <- Assessment data filled (assessment_report) or ASSESSMENT image
        RELEASED    <- Final GD uploaded (FINAL_GD)  [HC / Ex-Bond]
        INTO_BOND   <- Final GD uploaded for Into-Bond GDs
        CLEARED     <- Into-Bond only: gross weight settled via Ex-Bond (within tolerance)
    Manual DISPUTED is left untouched."""
    gd_id = cast(int, gd.gd_id)
    status = cast(Optional[str], gd.status)
    gd_type = cast(Optional[str], gd.gd_type)
    if status == "DISPUTED":
        return status
    stages = stages_for(gd)

    def idx(name):
        return stages.index(name) if name in stages else -1

    target = 0  # DRAFT
    if _has_attachment(gd_id, "GD_VIEW", db) or gd.gd_number or gd.document_path:
        target = max(target, idx("FILED"))
    if (gd.examination_report and str(gd.examination_report).strip()) or \
            _has_attachment(gd_id, "EXAMINATION", db):
        target = max(target, idx("EXAMINATION"))
    if (gd.assessment_report and str(gd.assessment_report).strip()) or \
            _has_attachment(gd_id, "ASSESSMENT", db):
        target = max(target, idx("ASSESSED"))
    if (gd_type or "") != "INTO_BOND":
        if gd.final_gd_uploaded or _has_attachment(gd_id, "FINAL_GD", db):
            target = max(target, len(stages) - 1)
    if (gd_type or "") == "INTO_BOND" and "INTO_BOND" in stages:
        if getattr(gd, "into_bond_gd_uploaded", False) or \
                _has_attachment(gd_id, "INTO_BOND_GD", db):
            target = max(target, idx("INTO_BOND"))
    if (gd_type or "") == "INTO_BOND" and "CLEARED" in stages:
        from modules.weboc.helpers.weboc_service import bond_summary
        bond = bond_summary(gd, db)
        if bond.get("applies") and bond.get("is_weight_settled"):
            target = max(target, idx("CLEARED"))

    cur = idx(status)
    if target > cur:
        status = stages[target]
        gd.status = status
        gd.updated_at = datetime.utcnow()
    return status or stages[0]


def derive_gd_type(data: GDSave, gd: Optional[GoodsDeclaration]) -> Optional[str]:
    """Resolve gd_type: explicit value wins, then Field 2 (declaration_type), then the
    gd_number prefix. data.gd_type is already validated to be a real GD_TYPES member or
    None by the schema."""
    if data.gd_type:
        return data.gd_type
    dt = (data.declaration_type or "").strip().upper()
    if dt in DECL_TYPE_MAP:
        return DECL_TYPE_MAP[dt]
    num = (data.gd_number or (gd.gd_number if gd else "") or "").upper()
    m = re.search(r"-(IB|HC|EX)-", num)
    if m:
        return DECL_TYPE_MAP[m.group(1)]
    return None


def _clean_levies(levies: Optional[List[LevyIn]]) -> Optional[list]:
    if not levies:
        return None
    out = []
    for lv in levies:
        if not lv.code and lv.rate is None and lv.amount_pkr is None:
            continue
        out.append({"code": lv.code, "rate": lv.rate, "amount_pkr": lv.amount_pkr})
    return out or None


def apply_gd_fields(gd: GoodsDeclaration, data: GDSave) -> None:
    fields_set = data.model_fields_set
    for field in _STR_FIELDS + _DATE_FIELDS:
        value = getattr(data, field)
        if value is not None:
            setattr(gd, field, value)
    for field in _DEC_FIELDS:
        # Present-in-payload clears the field even to None (a real customs duty figure
        # being explicitly blanked out), unlike string/date fields above.
        if field in fields_set:
            setattr(gd, field, getattr(data, field))
    for field in _INT_FIELDS:
        value = getattr(data, field)
        if field in fields_set and value is not None:
            setattr(gd, field, value)
    for field in _BOOL_FIELDS:
        value = getattr(data, field)
        if value is not None:
            setattr(gd, field, value)

    gt = derive_gd_type(data, gd)
    if gt:
        gd.gd_type = gt
    if "levy_breakdown" in fields_set:
        gd.levy_breakdown = _clean_levies(data.levy_breakdown)
    if data.status:
        gd.status = data.status
    gd.updated_at = datetime.utcnow()


def replace_items(gd: GoodsDeclaration, items: Optional[List[GDItemIn]], db: Session) -> None:
    """Rewrite this GD's item rows (delete + recreate), mirroring invoice/packing line items."""
    if items is None:
        return
    db.query(GDItem).filter(GDItem.gd_id == gd.gd_id).delete()
    for idx, item in enumerate(items, start=1):
        db.add(GDItem(
            gd_id=gd.gd_id,
            item_number=item.item_number or idx,
            hs_code=item.hs_code, goods_description=item.goods_description,
            quantity=item.quantity, unit_type=item.unit_type,
            country_of_origin=item.country_of_origin, sro_no=item.sro_no,
            quota_reference=item.quota_reference, m_size=item.m_size,
            unit_value_declared=item.unit_value_declared,
            unit_value_assessed=item.unit_value_assessed,
            total_value_declared_usd=item.total_value_declared_usd,
            total_value_assessed_usd=item.total_value_assessed_usd,
            custom_value_declared_pkr=item.custom_value_declared_pkr,
            custom_value_assessed_pkr=item.custom_value_assessed_pkr,
            levies=_clean_levies(item.levies),
        ))


def save_file(upload: UploadFile, gd_id: int) -> str:
    upload_dir = _gd_upload_dir()
    dest = safe_upload_path(upload_dir, gd_id, upload.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def normalize_gd(gd: GoodsDeclaration, db: Session) -> None:
    """Overwrite the GD importer/exporter names with their clean canonical values (same
    masters/rules as LC + contract normalization). Novel names are saved but flagged
    for review."""
    normalize_goods_declaration(gd, db)


def get_gd_or_404(gd_id: int, db: Session) -> GoodsDeclaration:
    gd = db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).first()
    if not gd:
        raise NotFoundError("GD not found")
    return gd


def apply_staged_document(gd: GoodsDeclaration, data: GDSave):
    if not data.staged_file:
        return
    dest, orig = promote_staged(
        STAGE_SUBDIR, data.staged_file, gd.gd_id,
        data.original_filename, ALLOWED_EXTENSIONS,
    )
    if dest:
        replace_document_path(gd, dest, orig)
    if data.raw_extracted_data is not None:
        gd.raw_extracted_data = data.raw_extracted_data


def get_or_create_placeholder(data: GDSave, db: Session, user_id: int) -> Tuple[GoodsDeclaration, bool]:
    """Returns (gd, is_new)."""
    if data.gd_id:
        gd = db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == data.gd_id).first()
        if not gd:
            raise NotFoundError("GD not found")
        return gd, False

    if data.shipment_id:
        gd = (db.query(GoodsDeclaration)
              .filter(GoodsDeclaration.shipment_id == data.shipment_id)
              .order_by(GoodsDeclaration.gd_id.desc()).first())
        if gd:
            return gd, False

    shipment = (db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first()
                if data.shipment_id else None)
    gd = GoodsDeclaration(
        shipment_id=data.shipment_id,
        lc_id=shipment.lc_id if shipment else None,
        source="UPLOADED" if data.staged_file else "MANUAL",
        status="FILED",
        created_by=user_id,
    )
    db.add(gd)
    db.flush()
    return gd, True


def save_gd(data: GDSave, db: Session, user_id: int) -> GoodsDeclaration:
    from infrastructure.activity.activity_service import log_activity

    gd, _is_new = get_or_create_placeholder(data, db, user_id)
    apply_gd_fields(gd, data)
    normalize_gd(gd, db)
    apply_staged_document(gd, data)
    replace_items(gd, data.items, db)
    # "Final GD" verify-save (from the WeBOC tab's AI extract flow) marks the GD Released.
    if data.final_gd:
        gd.final_gd_uploaded = True
    recompute_gd_status(gd, db)
    log_activity(db, gd.shipment_id, user_id, "UPLOAD", doc_type="Goods Declaration")
    db.commit()
    db.refresh(gd)
    return gd


def update_gd(gd_id: int, data: GDSave, db: Session, user_id: int) -> GoodsDeclaration:
    from infrastructure.activity.activity_service import log_activity
    from modules.weboc import services as weboc_svc

    gd = get_gd_or_404(gd_id, db)
    apply_gd_fields(gd, data)
    normalize_gd(gd, db)
    replace_items(gd, data.items, db)
    if data.pending_attachments:
        for att in data.pending_attachments:
            weboc_svc.commit_staged_attachment(
                gd, att.kind, att.staged_file, att.original_filename or "document.pdf",
                db, user_id, replace=False,
            )
    recompute_gd_status(gd, db)
    log_activity(db, gd.shipment_id, user_id, "EDIT", doc_type="Goods Declaration")
    db.commit()
    return gd


def delete_gd(gd_id: int, db: Session, deleted_by: int) -> None:
    from infrastructure.audit.audit_service import log_audit

    gd = get_gd_or_404(gd_id, db)
    log_audit(db, deleted_by, "DELETE_GD", entity_type="GOODS_DECLARATION", entity_id=gd.gd_id,
              old_value={"gd_number": gd.gd_number, "status": gd.status},
              description=f"Deleted GD '{gd.gd_number or gd.gd_id}'")
    doc_path = cast(Optional[str], gd.document_path)
    if doc_path and os.path.exists(doc_path):
        try:
            os.remove(doc_path)
        except OSError:
            pass
    db.delete(gd)
    db.commit()


def verify_gd(gd_id: int, user_id: int, db: Session) -> GoodsDeclaration:
    from infrastructure.activity.activity_service import log_activity

    gd = get_gd_or_404(gd_id, db)
    gd.is_verified = True
    gd.verified_at = datetime.utcnow()
    gd.verified_by = user_id
    log_activity(db, gd.shipment_id, user_id, "STATUS", doc_type="Goods Declaration", detail="Verified GD against origin data")
    db.commit()
    return gd


def unverify_gd(gd_id: int, user_id: int, db: Session) -> GoodsDeclaration:
    from infrastructure.activity.activity_service import log_activity

    gd = get_gd_or_404(gd_id, db)
    gd.is_verified = False
    gd.verified_at = None
    gd.verified_by = None
    log_activity(db, gd.shipment_id, user_id, "STATUS", doc_type="Goods Declaration", detail="Unverified GD")
    db.commit()
    return gd


def to_response(gd: GoodsDeclaration, db: Optional[Session] = None) -> dict:
    def n(v):
        return float(v) if v is not None else None

    doc_path = cast(Optional[str], gd.document_path)
    out: dict[str, Any] = {
        "gd_id": gd.gd_id, "shipment_id": gd.shipment_id, "lc_id": gd.lc_id,
        "document_filename": gd.document_filename,
        "has_document": bool(doc_path and os.path.exists(doc_path)),
        "status": gd.status,
        "gd_type": gd.gd_type,
        "lab_report_available": bool(gd.lab_report_available),
        "lab_report_assessment": bool(gd.lab_report_assessment),
        "final_gd_uploaded": bool(gd.final_gd_uploaded),
        "gd_view_uploaded": bool(gd.gd_view_uploaded),
        "item_details_uploaded": bool(gd.item_details_uploaded),
        "into_bond_gd_uploaded": bool(getattr(gd, "into_bond_gd_uploaded", False)),
        "is_verified": bool(getattr(gd, "is_verified", False)),
        "verified_at": gd.verified_at.isoformat() if getattr(gd, "verified_at", None) else None,
        "verified_by": getattr(gd, "verified_by", None),
    }
    for f in _STR_FIELDS:
        out[f] = getattr(gd, f)
    for f in _DATE_FIELDS:
        v = getattr(gd, f)
        out[f] = v.isoformat() if v else None
    for f in _DEC_FIELDS + _INT_FIELDS:
        out[f] = n(getattr(gd, f))
    out["levy_breakdown"] = gd.levy_breakdown or []
    out["items"] = [{
        "item_id": it.item_id, "item_number": it.item_number,
        "hs_code": it.hs_code, "goods_description": it.goods_description,
        "quantity": n(it.quantity), "unit_type": it.unit_type,
        "country_of_origin": it.country_of_origin, "sro_no": it.sro_no,
        "quota_reference": it.quota_reference, "m_size": it.m_size,
        "unit_value_declared": n(it.unit_value_declared),
        "unit_value_assessed": n(it.unit_value_assessed),
        "total_value_declared_usd": n(it.total_value_declared_usd),
        "total_value_assessed_usd": n(it.total_value_assessed_usd),
        "custom_value_declared_pkr": n(it.custom_value_declared_pkr),
        "custom_value_assessed_pkr": n(it.custom_value_assessed_pkr),
        "levies": it.levies or [],
    } for it in sorted(cast(list, gd.items), key=lambda x: x.item_number or 0)]
    stages = stages_for(gd)
    out["workflow_stages"] = stages
    status = cast(Optional[str], gd.status)
    gd_type = cast(Optional[str], gd.gd_type)
    nxt = None
    if status in stages and status != stages[-1]:
        nxt = stages[stages.index(status) + 1]
        if nxt == "CLEARED" and (gd_type or "") == "INTO_BOND":
            if db is None:
                nxt = None
            else:
                from modules.weboc.helpers.weboc_service import bond_summary
                if not bond_summary(gd, db).get("is_weight_settled"):
                    nxt = None
    out["next_status"] = nxt
    return out


def attachment_to_dict(att: GDAttachment) -> dict:
    return {
        "attachment_id": att.attachment_id, "gd_id": att.gd_id, "kind": att.kind,
        "filename": att.filename,
        "uploaded_at": att.uploaded_at.isoformat() if att.uploaded_at else None,
    }


def list_attachments(gd_id: int, kind: Optional[str], db: Session) -> List[GDAttachment]:
    q = db.query(GDAttachment).filter(GDAttachment.gd_id == gd_id)
    if kind:
        q = q.filter(GDAttachment.kind == kind.upper())
    return q.order_by(GDAttachment.attachment_id).all()


def add_attachment(gd_id: int, kind: str, file: UploadFile, user_id: int, db: Session) -> dict:
    from core.exceptions import ValidationError

    enforce_document_quota()
    gd = get_gd_or_404(gd_id, db)
    kind = kind.upper()
    if kind not in ATTACH_KINDS:
        raise ValidationError(f"kind must be one of {ATTACH_KINDS}")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"Only JPG, PNG, PDF supported. Got: {ext}")

    attach_dir = _attach_upload_dir()
    att = GDAttachment(gd_id=gd.gd_id, kind=kind, filename=file.filename, uploaded_by=user_id)
    db.add(att)
    db.flush()  # need attachment_id for the filename
    dest = safe_upload_path(attach_dir, att.attachment_id, file.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    att.file_path = dest
    db.flush()

    # Auto-advance the GD/WebOC status from the uploaded document (monotonic - never demotes).
    prev_status = gd.status
    recompute_gd_status(gd, db)
    status_changed = gd.status != prev_status

    db.commit()
    db.refresh(att)
    meter_document_accepted(file_path=att.file_path)
    out = attachment_to_dict(att)
    out["gd_status"] = gd.status
    out["status_changed"] = status_changed
    out["auto_filed"] = status_changed and gd.status == "FILED"
    return out


def get_attachment_or_404(attachment_id: int, db: Session) -> GDAttachment:
    att = db.query(GDAttachment).filter(GDAttachment.attachment_id == attachment_id).first()
    if not att:
        raise NotFoundError("Attachment not found")
    return att


def delete_attachment(attachment_id: int, db: Session) -> None:
    att = get_attachment_or_404(attachment_id, db)
    if att.file_path and os.path.exists(att.file_path):
        try:
            os.remove(att.file_path)
        except OSError:
            pass
    db.delete(att)
    db.commit()
