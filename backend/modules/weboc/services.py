"""Service orchestrator for WeBOC Goods Declaration operations.
"""
import os
import shutil
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from core.exceptions import NotFoundError, ConflictError, ValidationError
from models.database_models import (
    GoodsDeclaration, GDAttachment, GDItem, ExBondEntry, GdKgtlWeighment, Shipment
)
from modules.weboc.gd_service import (
    recompute_gd_status, ATTACH_DIR, ATTACH_STAGE_SUBDIR, ALLOWED_EXTENSIONS
)
from utils.staging import stage_bytes, staged_dir
from infrastructure.activity.activity_service import log_activity
from infrastructure.audit.audit_service import log_audit
from utils.uploads import safe_upload_path
from modules.weboc.helpers.bond_alerts import scan_bond_alerts
from infrastructure.normalization.normalization_service import normalize_goods_declaration
from modules.weboc.helpers.weboc_service import (
    classify_declaration, classify_from_gd_number, bond_summary,
    ex_bond_would_exceed, normalize_gd_filing_date, INTO_BOND_DAYS,
    resolve_gd_type_with_eb_guard,
)
from .schemas import (
    GDViewSave, ItemDetailsSave, IntoBondGDSave, ExBondGDSave, ExBondEntrySave,
    GdKgtlWeighmentSave, BondPenaltySave, DeclarationTypeOverride
)

# ---------------------------------------------------------------------------
# pure helper converters
# ---------------------------------------------------------------------------
def _f(v):
    return float(v) if v is not None else None


def _dec(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _str(v):
    return (str(v).strip() or None) if v not in (None, "") else None


def _date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Database retrieval helpers
# ---------------------------------------------------------------------------
def get_shipment_or_error(shipment_id: int, db: Session) -> Shipment:
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        raise NotFoundError("Shipment not found")
    return s


def get_gd_or_error(gd_id: int, db: Session) -> GoodsDeclaration:
    gd = db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).first()
    if not gd:
        raise NotFoundError("GD not found")
    return gd


def get_or_create_gd_for_shipment(shipment: Shipment, db: Session, user_id: int) -> GoodsDeclaration:
    gd = (db.query(GoodsDeclaration)
            .filter(GoodsDeclaration.shipment_id == shipment.shipment_id)
            .order_by(GoodsDeclaration.gd_id.desc()).first())
    if gd is None:
        gd = GoodsDeclaration(shipment_id=shipment.shipment_id, lc_id=shipment.lc_id,
                              source="UPLOADED", status="DRAFT", created_by=user_id)
        db.add(gd)
        db.flush()
    return gd


def resolve_gd_for_save(
    gd_id: Optional[int],
    shipment_id: Optional[int],
    db: Session,
    user_id: int,
) -> GoodsDeclaration:
    if gd_id:
        return get_gd_or_error(gd_id, db)
    if shipment_id:
        shipment = get_shipment_or_error(shipment_id, db)
        return get_or_create_gd_for_shipment(shipment, db, user_id)
    raise ValidationError("gd_id or shipment_id is required")


def stage_attachment_bytes(file_contents: bytes, filename: str) -> tuple[str, str]:
    return stage_bytes(file_contents, ATTACH_STAGE_SUBDIR, ALLOWED_EXTENSIONS, filename, perm_dir=ATTACH_DIR)


def commit_staged_attachment(
    gd: GoodsDeclaration,
    kind: str,
    staged_file: str,
    original_filename: str,
    db: Session,
    user_id: int,
    *,
    replace: bool = True,
) -> GDAttachment:
    stage_path = os.path.join(
        staged_dir(ATTACH_STAGE_SUBDIR, ATTACH_DIR), os.path.basename(staged_file))
    if not os.path.exists(stage_path):
        raise ValidationError("Staged document not found — please re-upload the file.")
    with open(stage_path, "rb") as f:
        contents = f.read()
    try:
        os.remove(stage_path)
    except OSError:
        pass
    orig = original_filename or "document.pdf"
    if replace:
        return replace_gd_attachment(gd, kind, orig, contents, db, user_id)
    return add_gd_attachment(gd, kind, orig, contents, db, user_id)


def replace_gd_attachment(gd: GoodsDeclaration, kind: str, filename: str, file_contents: bytes,
                           db: Session, user_id: int) -> GDAttachment:
    """Store the file as a GD attachment, replacing any previous file of the same kind."""
    old = db.query(GDAttachment).filter(
        GDAttachment.gd_id == gd.gd_id, GDAttachment.kind == kind).all()
    for a in old:
        if a.file_path and os.path.exists(a.file_path):
            try:
                os.remove(a.file_path)
            except OSError:
                pass
        db.delete(a)
    db.flush()

    os.makedirs(ATTACH_DIR, exist_ok=True)
    att = GDAttachment(gd_id=gd.gd_id, kind=kind, filename=filename,
                       uploaded_by=user_id)
    db.add(att)
    db.flush()
    dest = safe_upload_path(ATTACH_DIR, att.attachment_id, filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        f.write(file_contents)
    att.file_path = dest
    db.flush()
    return att


def add_gd_attachment(gd: GoodsDeclaration, kind: str, filename: str, file_contents: bytes,
                       db: Session, user_id: int) -> GDAttachment:
    """Append a new attachment."""
    os.makedirs(ATTACH_DIR, exist_ok=True)
    att = GDAttachment(gd_id=gd.gd_id, kind=kind, filename=filename,
                       uploaded_by=user_id)
    db.add(att)
    db.flush()
    dest = safe_upload_path(ATTACH_DIR, att.attachment_id, filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        f.write(file_contents)
    att.file_path = dest
    db.flush()
    return att


def get_gd_attachment_file(gd_id: int, kind: str, db: Session) -> GDAttachment:
    att = (db.query(GDAttachment)
             .filter(GDAttachment.gd_id == gd_id, GDAttachment.kind == kind)
             .order_by(GDAttachment.attachment_id.desc()).first())
    if not att or not att.file_path or not os.path.exists(att.file_path):
        raise NotFoundError("Document not found")
    return att


def normalize_importer(gd: GoodsDeclaration, db: Session):
    normalize_goods_declaration(gd, db)


# ---------------------------------------------------------------------------
# Form fallback payloads (shaped like extraction results)
# ---------------------------------------------------------------------------
GD_VIEW_STR = ["gd_number", "custom_office", "importer_name", "importer_ntn", "exporter_name",
               "vessel_name", "bl_number", "port_of_shipment", "port_of_discharge",
               "hs_code", "goods_description", "package_type", "country_of_origin"]
GD_VIEW_DATE = ["filing_date", "bl_date", "eta"]
GD_VIEW_DEC = ["gross_weight_mt", "net_weight_mt", "declared_value_pkr", "assessed_value_pkr",
               "declared_value_usd", "assessed_value_usd", "exchange_rate_pkr",
               "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
               "additional_customs_duty_pkr", "additional_sales_tax_pkr",
               "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr", "total_duties_pkr",
               "section82_penalty_pkr", "bonded_qty_mt"]
GD_VIEW_INT = ["package_count"]


def get_gd_view_current(gd: GoodsDeclaration) -> dict:
    out = {f: getattr(gd, f, None) for f in GD_VIEW_STR}
    out.update({f: (d.isoformat() if (d := getattr(gd, f, None)) else None)
                for f in GD_VIEW_DATE})
    out.update({f: _f(getattr(gd, f, None)) for f in GD_VIEW_DEC})
    out.update({f: getattr(gd, f, None) for f in GD_VIEW_INT})
    out["declaration_type"] = gd.declaration_type_raw
    out["gd_type"] = gd.gd_type
    return out


def get_item_details_current(gd: GoodsDeclaration, db: Session) -> dict:
    items = (db.query(GDItem).filter(GDItem.gd_id == gd.gd_id)
               .order_by(GDItem.item_number).all())
    return {
        "gd_number": gd.gd_number,
        "importer_name": gd.importer_name,
        "hs_code": gd.hs_code,
        "gross_weight_mt": _f(gd.gross_weight_mt),
        "net_weight_mt": _f(gd.net_weight_mt),
        "items": [{
            "item_number": it.item_number,
            "item_no": it.item_number,
            "hs_code": it.hs_code,
            "goods_description": it.goods_description,
            "description": it.goods_description,
            "quantity": _f(it.quantity),
            "unit": it.unit or it.unit_type,
            "unit_type": it.unit_type or it.unit,
            "declared_quantity": _f(it.declared_quantity),
            "assessed_quantity": _f(it.assessed_quantity),
            "country_of_origin": it.country_of_origin,
            "sro_no": it.sro_no,
            "sro_number": it.sro_no,
            "quota_reference": it.quota_reference,
            "m_size": it.m_size,
            "unit_value_declared": _f(it.unit_value_declared),
            "unit_value_assessed": _f(it.unit_value_assessed),
            "total_value_declared_usd": _f(it.total_value_declared_usd),
            "total_value_assessed_usd": _f(it.total_value_assessed_usd),
            "custom_value_declared_pkr": _f(it.custom_value_declared_pkr),
            "custom_value_assessed_pkr": _f(it.custom_value_assessed_pkr),
        } for it in items],
    }


def get_ex_bond_gd_current(gd: GoodsDeclaration) -> dict:
    return {
        "gd_number": None, "filing_date": None, "declaration_type": "EX",
        "gd_type": "EX_BOND", "gross_weight_mt": None, "net_weight_mt": None,
        "quantity_mt": None,
    }


IB_GD_STR = ["gd_number", "machine_number", "custom_office", "importer_name", "importer_ntn",
             "exporter_name", "vessel_name", "bl_number", "hs_code", "goods_description",
             "package_type", "girm_egm_number"]
IB_GD_DATE = ["filing_date", "bl_date", "assessment_date"]
IB_GD_DEC = ["gross_weight_mt", "net_weight_mt", "bonded_qty_mt", "total_duties_pkr"]
IB_GD_INT = ["package_count"]


def get_into_bond_gd_current(gd: GoodsDeclaration) -> dict:
    out = {f: getattr(gd, f, None) for f in IB_GD_STR}
    out.update({f: (d.isoformat() if (d := getattr(gd, f, None)) else None)
                for f in IB_GD_DATE})
    out.update({f: _f(getattr(gd, f, None)) for f in IB_GD_DEC})
    out.update({f: getattr(gd, f, None) for f in IB_GD_INT})
    out["declaration_type"] = gd.declaration_type_raw or "IB"
    out["gd_type"] = "INTO_BOND"
    return out


# ---------------------------------------------------------------------------
# Business logic mapping and validation
# ---------------------------------------------------------------------------
def _clean_charges(rows):
    if not isinstance(rows, list):
        return None
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = _str(r.get("code"))
        desc = _str(r.get("description"))
        amt = _f(_dec(r.get("amount_pkr")))
        if not code and not desc and amt is None:
            continue
        out.append({"code": code, "description": desc, "amount_pkr": amt})
    return out or None


def _bonded_qty_from(data: dict):
    q = _dec(data.get("bonded_qty_mt"))
    if q is not None and q > 0:
        return q
    for k in ("gross_weight_mt", "net_weight_mt"):
        v = _dec(data.get(k))
        if v is not None and v > 0:
            return v
    return None


def apply_gd_view(gd: GoodsDeclaration, data: GDViewSave, db: Session):
    data_dict = data.model_dump(exclude_unset=False)
    for f in GD_VIEW_STR:
        if f in data_dict and data_dict[f] not in (None, ""):
            setattr(gd, f, _str(data_dict[f]))
    for f in GD_VIEW_DATE:
        if data_dict.get(f):
            d = _date(data_dict[f])
            if d:
                setattr(gd, f, d)
    for f in GD_VIEW_DEC:
        if f in data_dict:
            v = _dec(data_dict[f])
            if v is not None:
                setattr(gd, f, v)
    # The GD View often prints itemized duty/tax/charge rows with no single combined total
    # line — fall back to summing the components (same rule already used for Ex-Bond entries
    # in _duty_from_extracted) rather than leaving the report's duty position blank.
    if gd.total_duties_pkr is None:
        gd.total_duties_pkr = _duty_from_extracted(data_dict)
    for f in GD_VIEW_INT:
        if f in data_dict and data_dict[f] not in (None, ""):
            setattr(gd, f, _int(data_dict[f]))
    if "charges_breakdown" in data_dict and data_dict["charges_breakdown"] is not None:
        gd.charges_breakdown = _clean_charges(data_dict["charges_breakdown"])

    raw = data_dict.get("declaration_type")
    if raw:
        gd.declaration_type_raw = _str(raw)
    candidate = None
    if data_dict.get("gd_type") in ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND", "UNKNOWN"):
        candidate = data_dict["gd_type"]
    elif raw:
        candidate = classify_declaration(raw)
    if (candidate or "UNKNOWN") == "UNKNOWN":
        from_no = classify_from_gd_number(data_dict.get("gd_number") or gd.gd_number)
        if from_no != "UNKNOWN":
            candidate = from_no
    if candidate:
        # EB can never exist without an IB — see resolve_gd_type_with_eb_guard.
        gd.gd_type = resolve_gd_type_with_eb_guard(gd, candidate)

    pen = gd.section82_penalty_pkr
    if pen is not None and float(pen) > 0:
        gd.late_filed = True

    if gd.gd_type == "INTO_BOND" and gd.bonded_qty_mt is None:
        gd.bonded_qty_mt = _bonded_qty_from(data_dict)

    gd.gd_view_uploaded = True
    gd.updated_at = datetime.utcnow()
    normalize_importer(gd, db)


def apply_item_details(gd: GoodsDeclaration, data: ItemDetailsSave, db: Session):
    data_dict = data.model_dump(exclude_unset=False)
    items = data_dict.get("items") or []
    if items:
        db.query(GDItem).filter(GDItem.gd_id == gd.gd_id).delete()
        for idx, it in enumerate(items, start=1):
            if not isinstance(it, dict):
                continue
            qty = _dec(it.get("quantity"))
            decl = _dec(it.get("declared_quantity"))
            db.add(GDItem(
                gd_id=gd.gd_id,
                item_number=_int(it.get("item_number")) or idx,
                hs_code=_str(it.get("hs_code")),
                goods_description=_str(it.get("goods_description")),
                quantity=qty if qty is not None else decl,
                unit_type=_str(it.get("unit")),
                unit=_str(it.get("unit")),
                declared_quantity=decl if decl is not None else qty,
                assessed_quantity=_dec(it.get("assessed_quantity")),
                country_of_origin=_str(it.get("country_of_origin")),
                sro_no=_str(it.get("sro_no")),
                quota_reference=_str(it.get("quota_reference")),
                m_size=_str(it.get("m_size")),
                unit_value_declared=_dec(it.get("unit_value_declared")),
                unit_value_assessed=_dec(it.get("unit_value_assessed")),
                total_value_declared_usd=_dec(it.get("total_value_declared_usd")),
                total_value_assessed_usd=_dec(it.get("total_value_assessed_usd")),
                custom_value_declared_pkr=_dec(it.get("custom_value_declared_pkr")),
                custom_value_assessed_pkr=_dec(it.get("custom_value_assessed_pkr")),
            ))
    for f in ("gross_weight_mt", "net_weight_mt"):
        if data_dict.get(f) is not None and getattr(gd, f) is None:
            setattr(gd, f, _dec(data_dict[f]))
    if data_dict.get("hs_code") and not gd.hs_code:
        gd.hs_code = _str(data_dict["hs_code"])
    gd.item_details_uploaded = True
    gd.updated_at = datetime.utcnow()


def apply_into_bond_gd(gd: GoodsDeclaration, data: IntoBondGDSave, db: Session):
    data_dict = data.model_dump(exclude_unset=False)
    normalize_gd_filing_date(data_dict)
    for f in IB_GD_STR:
        if f in data_dict and data_dict[f] not in (None, ""):
            setattr(gd, f, _str(data_dict[f]))
    for f in IB_GD_DATE:
        if data_dict.get(f):
            d = _date(data_dict[f])
            if d:
                setattr(gd, f, d)
    for f in IB_GD_DEC:
        if f in data_dict:
            v = _dec(data_dict[f])
            if v is not None:
                setattr(gd, f, v)
    # Same fallback as apply_gd_view() — harmless no-op today since IntoBondGDSave carries no
    # itemized components, but keeps this path consistent if that ever changes.
    if gd.total_duties_pkr is None:
        gd.total_duties_pkr = _duty_from_extracted(data_dict)
    for f in IB_GD_INT:
        if f in data_dict and data_dict[f] not in (None, ""):
            setattr(gd, f, _int(data_dict[f]))
    raw = data_dict.get("declaration_type")
    if raw:
        gd.declaration_type_raw = _str(raw)
    gd.gd_type = "INTO_BOND"
    if gd.bonded_qty_mt is None:
        gd.bonded_qty_mt = _bonded_qty_from(data_dict)
    gd.into_bond_gd_uploaded = True
    gd.updated_at = datetime.utcnow()
    normalize_importer(gd, db)


# ---------------------------------------------------------------------------
# API orchestration CRUD methods
# ---------------------------------------------------------------------------
def save_gd_view(data: GDViewSave, user_id: int, db: Session) -> GoodsDeclaration:
    gd = resolve_gd_for_save(data.gd_id, data.shipment_id, db, user_id)
    if data.staged_file:
        commit_staged_attachment(
            gd, "GD_VIEW", data.staged_file, data.original_filename or "gd_view.pdf",
            db, user_id,
        )
    apply_gd_view(gd, data, db)
    recompute_gd_status(gd, db)
    log_activity(db, gd.shipment_id, user_id, "UPLOAD", doc_type="GD View")
    db.commit()
    db.refresh(gd)
    return gd


def save_item_details(data: ItemDetailsSave, user_id: int, db: Session) -> GoodsDeclaration:
    gd = resolve_gd_for_save(data.gd_id, data.shipment_id, db, user_id)
    if data.staged_file:
        commit_staged_attachment(
            gd, "ITEM_DETAILS", data.staged_file, data.original_filename or "item_details.pdf",
            db, user_id,
        )
    apply_item_details(gd, data, db)
    recompute_gd_status(gd, db)
    log_activity(db, gd.shipment_id, user_id, "UPLOAD", doc_type="Item Details")
    db.commit()
    db.refresh(gd)
    return gd


def save_into_bond_gd(data: IntoBondGDSave, user_id: int, db: Session) -> GoodsDeclaration:
    gd = resolve_gd_for_save(data.gd_id, data.shipment_id, db, user_id)
    if not gd.gd_view_uploaded and not gd.gd_number:
        raise ValidationError("Upload the GD View before the Into-Bond GD.")
    if data.staged_file:
        commit_staged_attachment(
            gd, "INTO_BOND_GD", data.staged_file, data.original_filename or "into_bond_gd.pdf",
            db, user_id,
        )
    apply_into_bond_gd(gd, data, db)
    recompute_gd_status(gd, db)
    log_activity(db, gd.shipment_id, user_id, "UPLOAD", doc_type="Into-Bond GD")
    db.commit()
    db.refresh(gd)
    return gd


def _lift_qty_from_extracted(data: dict):
    q = _dec(data.get("quantity_mt"))
    if q is not None and q > 0:
        return q
    for k in ("net_weight_mt", "gross_weight_mt"):
        v = _dec(data.get(k))
        if v is not None and v > 0:
            return v
    items = data.get("items") or []
    total = Decimal("0")
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = _dec(it.get("quantity"))
        if qty is None or qty <= 0:
            continue
        unit = str(it.get("unit_type") or it.get("unit") or "MT").upper()
        if unit in ("KG", "KGS", "KILOGRAM", "KILOGRAMS"):
            total += qty / Decimal("1000")
        else:
            total += qty
    return total if total > 0 else None


def _duty_from_extracted(data: dict):
    v = _dec(data.get("total_duties_pkr"))
    if v is not None and v > 0:
        return v
    total = Decimal("0")
    for k in ("customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
              "additional_customs_duty_pkr", "additional_sales_tax_pkr",
              "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr"):
        part = _dec(data.get(k))
        if part is not None and part > 0:
            total += part
    return total if total > 0 else None


def _require_into_bond_filed(gd: GoodsDeclaration, db: Session) -> None:
    """EB can never exist without an IB. gd_type == INTO_BOND alone isn't enough (a GD
    can be flagged Into-Bond via the manual declaration-type override without the actual
    Into-Bond GD document ever having been filed) — require the document itself."""
    if gd.into_bond_gd_uploaded:
        return
    try:
        get_gd_attachment_file(gd.gd_id, "INTO_BOND_GD", db)
    except NotFoundError:
        raise ValidationError(
            "Upload the Into-Bond (IB) GD first — an Ex-Bond release can only be "
            "recorded against an existing Into-Bond GD."
        )


def save_ex_bond_gd(data: ExBondGDSave, user_id: int, db: Session) -> ExBondEntry:
    gd = resolve_gd_for_save(data.gd_id, data.shipment_id, db, user_id)
    if (gd.gd_type or "") != "INTO_BOND":
        raise ValidationError("Ex-Bond GDs apply only to Into-Bond shipments.")
    _require_into_bond_filed(gd, db)

    data_dict = data.model_dump(exclude_unset=False)
    qty = _dec(data_dict.get("quantity_mt")) or _lift_qty_from_extracted(data_dict)
    if qty is None or qty <= 0:
        raise ValidationError("Quantity lifted (MT) is required.")

    exceeds, remaining, over_by = ex_bond_would_exceed(gd, db, qty)
    if exceeds and not data_dict.get("force"):
        raise ConflictError(
            f"This lifting of {float(qty):,.3f} MT exceeds the bonded quantity by "
            f"{over_by:,.3f} MT (only {remaining:,.3f} MT remains). "
            f"Correct the quantity, or confirm to record it anyway."
        )

    att_id = _int(data_dict.get("attachment_id"))
    if data.staged_file:
        att = commit_staged_attachment(
            gd, "EX_BOND_GD", data.staged_file, data.original_filename or "ex_bond_gd.pdf",
            db, user_id, replace=False,
        )
        att_id = att.attachment_id
    if att_id:
        dup = db.query(ExBondEntry).filter(
            ExBondEntry.into_bond_gd_id == gd.gd_id,
            ExBondEntry.attachment_id == att_id).first()
        if dup:
            raise ConflictError(
                "This Ex-Bond document was already recorded — delete the duplicate entry if needed."
            )

    normalize_gd_filing_date(data_dict)
    duty = _dec(data_dict.get("duties_paid_pkr"))
    duty_source = "MANUAL" if duty is not None else None
    if duty is None:
        duty = _duty_from_extracted(data_dict)
        duty_source = "DOCUMENT" if duty is not None else None

    e = ExBondEntry(
        into_bond_gd_id=gd.gd_id, shipment_id=gd.shipment_id,
        gd_number=_str(data_dict.get("gd_number")),
        ex_bond_date=_date(data_dict.get("filing_date") or data_dict.get("ex_bond_date")),
        quantity_mt=qty,
        duties_paid_pkr=duty,
        duty_source=duty_source,
        remarks=_str(data_dict.get("remarks")) or "From Ex-Bond GD upload",
        attachment_id=att_id,
        created_by=user_id)
    db.add(e)
    db.flush()
    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    log_activity(db, gd.shipment_id, user_id, "UPLOAD", doc_type="Ex-Bond GD")
    db.commit()
    return e


def add_ex_bond_entry(gd_id: int, data: ExBondEntrySave, user_id: int, db: Session) -> ExBondEntry:
    gd = get_gd_or_error(gd_id, db)
    if (gd.gd_type or "") != "INTO_BOND":
        raise ValidationError("Ex-bond entries can only be recorded against an Into-Bond GD.")
    _require_into_bond_filed(gd, db)

    data_dict = data.model_dump(exclude_unset=False)
    qty = _dec(data_dict.get("quantity_mt"))
    if qty is None or qty <= 0:
        raise ValidationError("Quantity lifted (MT) is required.")

    exceeds, remaining, over_by = ex_bond_would_exceed(gd, db, qty)
    if exceeds and not data_dict.get("force"):
        raise ConflictError(
            f"This lifting of {float(qty):,.3f} MT exceeds the bonded quantity by "
            f"{over_by:,.3f} MT (only {remaining:,.3f} MT remains in bond). "
            f"Correct the quantity, or confirm to record it anyway."
        )

    duty = _dec(data_dict.get("duties_paid_pkr"))
    e = ExBondEntry(
        into_bond_gd_id=gd.gd_id, shipment_id=gd.shipment_id,
        gd_number=_str(data_dict.get("gd_number")),
        ex_bond_date=_date(data_dict.get("ex_bond_date")),
        quantity_mt=qty, remarks=_str(data_dict.get("remarks")),
        duties_paid_pkr=duty,
        duty_source="MANUAL" if duty is not None else None,
        created_by=user_id)
    db.add(e)
    db.flush()
    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    db.commit()
    return e


def update_ex_bond_entry(entry_id: int, data: ExBondEntrySave, db: Session) -> ExBondEntry:
    e = db.query(ExBondEntry).filter(ExBondEntry.entry_id == entry_id).first()
    if not e:
        raise NotFoundError("Ex-bond entry not found")
    gd = get_gd_or_error(e.into_bond_gd_id, db)

    data_dict = data.model_dump(exclude_unset=False)
    if "quantity_mt" in data_dict and data_dict["quantity_mt"] is not None:
        qty = _dec(data_dict.get("quantity_mt"))
        if qty is None or qty <= 0:
            raise ValidationError("Quantity lifted (MT) is required.")
        exceeds, remaining, over_by = ex_bond_would_exceed(gd, db, qty, exclude_entry_id=entry_id)
        if exceeds and not data_dict.get("force"):
            raise ConflictError(
                f"This lifting of {float(qty):,.3f} MT exceeds the bonded quantity by "
                f"{over_by:,.3f} MT."
            )
        e.quantity_mt = qty

    if "gd_number" in data_dict:
        e.gd_number = _str(data_dict.get("gd_number"))
    if "ex_bond_date" in data_dict:
        e.ex_bond_date = _date(data_dict.get("ex_bond_date"))
    if "remarks" in data_dict:
        e.remarks = _str(data_dict.get("remarks"))

    if "duties_paid_pkr" in data_dict:
        v = data_dict.get("duties_paid_pkr")
        if v in (None, ""):
            e.duties_paid_pkr, e.duty_source = None, None
        else:
            amt = _dec(v)
            if amt is None or amt < 0:
                raise ValidationError("Duty paid must be a positive amount in PKR.")
            e.duties_paid_pkr = amt
            e.duty_source = "MANUAL"

    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    db.commit()
    return e


def delete_ex_bond_entry(entry_id: int, db: Session, deleted_by: int) -> GoodsDeclaration:
    e = db.query(ExBondEntry).filter(ExBondEntry.entry_id == entry_id).first()
    if not e:
        raise NotFoundError("Ex-bond entry not found")
    gd_id = e.into_bond_gd_id
    att_id = e.attachment_id
    log_audit(db, deleted_by, "DELETE_EX_BOND_ENTRY", entity_type="EX_BOND_ENTRY", entity_id=e.entry_id,
              old_value={"into_bond_gd_id": gd_id, "gd_number": e.gd_number},
              description=f"Deleted ex-bond entry {e.entry_id} (into-bond GD {gd_id})")
    db.delete(e)
    if att_id:
        att = db.query(GDAttachment).filter(GDAttachment.attachment_id == att_id).first()
        if att:
            if att.file_path and os.path.exists(att.file_path):
                try:
                    os.remove(att.file_path)
                except OSError:
                    pass
            db.delete(att)
    db.commit()
    gd = get_gd_or_error(gd_id, db)
    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    db.commit()
    return gd


def add_kgtl_weighment(gd_id: int, data: GdKgtlWeighmentSave, user_id: int, db: Session) -> GdKgtlWeighment:
    gd = get_gd_or_error(gd_id, db)
    data_dict = data.model_dump(exclude_unset=False)
    own = _dec(data_dict.get("own_weight_kg"))
    kgtl = _dec(data_dict.get("kgtl_weight_kg"))
    if own is None and kgtl is None:
        raise ValidationError("Enter at least one weight (own or KGTL) in KG.")

    row = GdKgtlWeighment(
        gd_id=gd.gd_id,
        vehicle_number=_str(data_dict.get("vehicle_number")),
        weigh_date=_date(data_dict.get("weigh_date")),
        own_weight_kg=own,
        kgtl_weight_kg=kgtl,
        own_coils=data_dict.get("own_coils"),
        kgtl_coils=data_dict.get("kgtl_coils"),
        remarks=_str(data_dict.get("remarks")),
        created_by=user_id,
    )
    db.add(row)
    db.commit()
    return row


def update_kgtl_weighment(weighment_id: int, data: GdKgtlWeighmentSave, db: Session) -> GdKgtlWeighment:
    row = db.query(GdKgtlWeighment).filter(GdKgtlWeighment.weighment_id == weighment_id).first()
    if not row:
        raise NotFoundError("Weighment not found")

    # exclude_unset is required here: this is a partial update (edit one field, e.g. coils,
    # leave the rest alone). With exclude_unset=False every declared field is always "in
    # data_dict" (Pydantic fills in its None default), so the checks below would always be
    # true and silently null out weight/vehicle/date/remarks that the caller never touched.
    data_dict = data.model_dump(exclude_unset=True)
    if "own_weight_kg" in data_dict:
        row.own_weight_kg = _dec(data_dict.get("own_weight_kg"))
    if "kgtl_weight_kg" in data_dict:
        row.kgtl_weight_kg = _dec(data_dict.get("kgtl_weight_kg"))
    if "own_coils" in data_dict:
        row.own_coils = data_dict.get("own_coils")
    if "kgtl_coils" in data_dict:
        row.kgtl_coils = data_dict.get("kgtl_coils")
    if "vehicle_number" in data_dict:
        row.vehicle_number = _str(data_dict.get("vehicle_number"))
    if "weigh_date" in data_dict:
        row.weigh_date = _date(data_dict.get("weigh_date"))
    if "remarks" in data_dict:
        row.remarks = _str(data_dict.get("remarks"))

    db.commit()
    return row


def delete_kgtl_weighment(weighment_id: int, db: Session, deleted_by: int) -> GoodsDeclaration:
    row = db.query(GdKgtlWeighment).filter(GdKgtlWeighment.weighment_id == weighment_id).first()
    if not row:
        raise NotFoundError("Weighment not found")
    gd_id = row.gd_id
    log_audit(db, deleted_by, "DELETE_KGTL_WEIGHMENT", entity_type="GD_KGTL_WEIGHMENT",
              entity_id=row.weighment_id, old_value={"gd_id": gd_id},
              description=f"Deleted KGTL weighment {row.weighment_id} (GD {gd_id})")
    db.delete(row)
    db.commit()
    return get_gd_or_error(gd_id, db)


def set_bond_penalty(gd_id: int, data: BondPenaltySave, user_id: int, db: Session):
    gd = get_gd_or_error(gd_id, db)
    if (gd.gd_type or "") != "INTO_BOND":
        raise ValidationError("A bond penalty only applies to an Into-Bond GD.")

    data_dict = data.model_dump(exclude_unset=False)
    amount = _dec(data_dict.get("penalty_pkr"))
    if amount is None or amount < 0:
        raise ValidationError("A penalty amount (PKR) is required.")

    source = str(data_dict.get("source") or "MANUAL").strip().upper()
    if source not in ("DOCUMENT", "MANUAL"):
        raise ValidationError("Source must be DOCUMENT or MANUAL.")

    reason = _str(data_dict.get("reason"))
    if source == "MANUAL" and not reason:
        raise ValidationError("A reason is required when entering a penalty manually.")

    bond = bond_summary(gd, db)
    if not bond.get("penalty_applies") and not data_dict.get("force"):
        raise ConflictError(
            f"This bond is not in breach of the {INTO_BOND_DAYS}-day deadline "
            f"(state {bond.get('state')}"
            + (f", settled {bond['settlement_date']} against a {bond['deadline']} "
               f"deadline" if bond.get("settlement_date") else "")
            + "), so no penalty is due. Confirm to record one anyway."
        )

    gd.bond_penalty_pkr = amount
    gd.bond_penalty_source = source
    gd.bond_penalty_reason = reason
    gd.bond_penalty_days = bond.get("penalty_days")
    gd.bond_penalty_recorded_at = datetime.utcnow()
    gd.bond_penalty_recorded_by = user_id
    db.commit()


def clear_bond_penalty(gd_id: int, db: Session):
    gd = get_gd_or_error(gd_id, db)
    gd.bond_penalty_pkr = None
    gd.bond_penalty_source = None
    gd.bond_penalty_reason = None
    gd.bond_penalty_days = None
    gd.bond_penalty_recorded_at = None
    gd.bond_penalty_recorded_by = None
    db.commit()


def set_declaration_type(gd_id: int, data: DeclarationTypeOverride, db: Session):
    gd = get_gd_or_error(gd_id, db)
    data_dict = data.model_dump(exclude_unset=False)
    t = str(data_dict.get("gd_type") or "").strip().upper()
    if t not in ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND", "UNKNOWN"):
        raise ValidationError("Invalid declaration type.")
    # EB can never exist without an IB — see resolve_gd_type_with_eb_guard.
    gd.gd_type = resolve_gd_type_with_eb_guard(gd, t)
    if t == "INTO_BOND" and gd.bonded_qty_mt is None:
        gd.bonded_qty_mt = gd.gross_weight_mt or gd.net_weight_mt
    if "bonded_qty_mt" in data_dict:
        q = _dec(data_dict.get("bonded_qty_mt"))
        if q is not None:
            gd.bonded_qty_mt = q
    gd.updated_at = datetime.utcnow()
    db.commit()
    recompute_gd_status(gd, db)
    db.commit()


def _resolve_gd_type_from_data(data: dict) -> str:
    t = classify_declaration(data.get("declaration_type"))
    if t != "UNKNOWN":
        return t
    return classify_from_gd_number(data.get("gd_number"))

