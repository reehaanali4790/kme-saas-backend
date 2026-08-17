"""Service orchestrator for Partial GD (EB Release) operations.

"Partial GD" is the business term; technically each one is an Ex-Bond Release linked to
an existing Into-Bond GD (`ExBondEntry`). Unlike the legacy single-document Ex-Bond GD
flow (still supported unchanged in services.py, for backward compatibility), a Partial
GD carries its own EB GD View and its own (potentially several) Item Detail documents,
and the SRO number is always extracted from THIS Partial GD's own item details — never
from the Into-Bond GD's item details.
"""
import os
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError, ConflictError, ValidationError
from models.database_models import (
    ExBondEntry, ExBondItem, GDAttachment, GoodsDeclaration, EdbApproval, SroGroupNumber,
)
from modules.weboc.gd_service import recompute_gd_status, attach_upload_dir, ALLOWED_EXTENSIONS
from modules.weboc.services import (
    get_gd_or_error, _require_into_bond_filed, _f, _dec, _int, _str, _date,
)
from modules.weboc.helpers.weboc_service import ex_bond_would_exceed
from modules.weboc.helpers.sro_usage import _refs_in, _approval_refs
from modules.weboc.helpers.bond_alerts import scan_bond_alerts
from infrastructure.audit.audit_service import log_audit
from utils.uploads import safe_upload_path
from .schemas import GDItemIn, PartialGdViewIn, PartialGdItemDetailsIn, PartialGdValidateApproval


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def get_entry_or_error(entry_id: int, db: Session) -> ExBondEntry:
    e = db.query(ExBondEntry).filter(ExBondEntry.entry_id == entry_id).first()
    if not e:
        raise NotFoundError("Partial GD not found")
    return e


def get_entry_attachment_file(entry_id: int, attachment_id: int, db: Session) -> GDAttachment:
    att = db.query(GDAttachment).filter(
        GDAttachment.attachment_id == attachment_id,
        GDAttachment.ex_bond_entry_id == entry_id).first()
    if not att or not att.file_path or not os.path.exists(att.file_path):
        raise NotFoundError("Document not found")
    return att


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def start_partial_gd(into_bond_gd_id: int, user_id: int, db: Session) -> dict:
    """Prepare a new Partial GD draft without creating a database row."""
    gd = get_gd_or_error(into_bond_gd_id, db)
    if (gd.gd_type or "") != "INTO_BOND":
        raise ValidationError(
            "Partial GDs (EB releases) can only be created against an Into-Bond GD."
        )
    _require_into_bond_filed(gd, db)
    return {"into_bond_gd_id": gd.gd_id, "draft": True}


def find_duplicate_gd_number_for_ib_gd(
    into_bond_gd_id: int,
    gd_number: Optional[str],
    db: Session,
    *,
    exclude_entry_id: Optional[int] = None,
) -> Optional[ExBondEntry]:
    """Check duplicate EB GD number before validate, scoped to an Into-Bond GD."""
    if not gd_number or not str(gd_number).strip():
        return None
    norm = str(gd_number).strip().upper()
    q = (db.query(ExBondEntry)
           .filter(ExBondEntry.into_bond_gd_id == into_bond_gd_id,
                   func.upper(func.btrim(ExBondEntry.gd_number)) == norm))
    if exclude_entry_id is not None:
        q = q.filter(ExBondEntry.entry_id != exclude_entry_id)
    return q.first()


def create_partial_gd_entry(into_bond_gd_id: int, user_id: int, db: Session) -> ExBondEntry:
    gd = get_gd_or_error(into_bond_gd_id, db)
    entry = ExBondEntry(
        into_bond_gd_id=gd.gd_id, shipment_id=gd.shipment_id,
        is_finalized=False, created_by=user_id,
    )
    db.add(entry)
    db.flush()
    return entry


def validate_partial_gd_approval_for_ib(
    into_bond_gd_id: int,
    data: PartialGdValidateApproval,
    user_id: int,
    db: Session,
) -> ExBondEntry:
    """Create a Partial GD entry and validate it in one transaction."""
    get_gd_or_error(into_bond_gd_id, db)
    entry = create_partial_gd_entry(into_bond_gd_id, user_id, db)
    return validate_partial_gd_approval(entry.entry_id, data, user_id, db)


# ---------------------------------------------------------------------------
# Attachments (scoped to one Partial GD via ex_bond_entry_id, not just gd_id)
# ---------------------------------------------------------------------------
def replace_entry_attachment(entry: ExBondEntry, kind: str, filename: str, file_contents: bytes,
                              db: Session, user_id: int) -> GDAttachment:
    """Store the file, replacing any previous attachment of the same kind on this entry
    (used for the EB GD View — one current document, like the IB's own GD_VIEW)."""
    old = db.query(GDAttachment).filter(
        GDAttachment.ex_bond_entry_id == entry.entry_id, GDAttachment.kind == kind).all()
    for a in old:
        if a.file_path and os.path.exists(a.file_path):
            try:
                os.remove(a.file_path)
            except OSError:
                pass
        db.delete(a)
    db.flush()

    attach_dir = attach_upload_dir()
    att = GDAttachment(gd_id=entry.into_bond_gd_id, ex_bond_entry_id=entry.entry_id,
                       kind=kind, filename=filename, uploaded_by=user_id)
    db.add(att)
    db.flush()
    dest = safe_upload_path(attach_dir, att.attachment_id, filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        f.write(file_contents)
    att.file_path = dest
    db.flush()
    return att


def add_entry_attachment(entry: ExBondEntry, kind: str, filename: str, file_contents: bytes,
                          db: Session, user_id: int) -> GDAttachment:
    """Append a new attachment (used for Item Details — several are expected per
    Partial GD, so each upload adds a new document rather than replacing the last)."""
    attach_dir = attach_upload_dir()
    att = GDAttachment(gd_id=entry.into_bond_gd_id, ex_bond_entry_id=entry.entry_id,
                       kind=kind, filename=filename, uploaded_by=user_id)
    db.add(att)
    db.flush()
    dest = safe_upload_path(attach_dir, att.attachment_id, filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        f.write(file_contents)
    att.file_path = dest
    db.flush()
    return att


# ---------------------------------------------------------------------------
# Apply extracted / manually entered data
# ---------------------------------------------------------------------------
def apply_partial_gd_view(entry: ExBondEntry, data: PartialGdViewIn) -> None:
    d = data.model_dump(exclude_unset=False)
    if d.get("gd_number"):
        entry.gd_number = d["gd_number"]
    ex_bond_date = d.get("ex_bond_date") or d.get("filing_date")
    if ex_bond_date:
        entry.ex_bond_date = ex_bond_date
    if entry.quantity_mt is None:
        qty = d.get("quantity_mt") or d.get("net_weight_mt") or d.get("gross_weight_mt")
        if qty is not None:
            entry.quantity_mt = qty
    if d.get("remarks"):
        entry.remarks = d["remarks"]


def _entry_item_qty_mt(it: ExBondItem) -> Optional[Decimal]:
    """Quantity an item consumes, in MT: assessed, else declared, else plain quantity —
    same convention as sro_usage.item_qty_mt (mirrored here since it operates on
    ExBondItem, not GDItem)."""
    q = it.assessed_quantity
    if q is None:
        q = it.declared_quantity
    if q is None:
        q = it.quantity
    if q is None:
        return None
    unit = (it.unit or "").strip().upper()
    val = Decimal(str(q))
    if unit.startswith("KG"):
        val = val / Decimal("1000")
    return val


def apply_partial_gd_item_details(entry: ExBondEntry, items: List[GDItemIn],
                                   source_attachment_id: int, db: Session) -> int:
    """Append this Item Details document's items to the Partial GD (never replaces
    earlier uploads — multiple Item Detail documents are expected per Partial GD)."""
    count = 0
    for idx, it in enumerate(items or [], start=1):
        db.add(ExBondItem(
            entry_id=entry.entry_id,
            source_attachment_id=source_attachment_id,
            item_number=it.item_number or idx,
            hs_code=it.hs_code,
            goods_description=it.goods_description,
            quantity=it.quantity,
            unit=it.unit or it.unit_type,
            declared_quantity=it.declared_quantity,
            assessed_quantity=it.assessed_quantity,
            country_of_origin=it.country_of_origin,
            sro_no=it.sro_no,
            quota_reference=it.quota_reference,
            m_size=it.m_size,
            unit_value_declared=it.unit_value_declared,
            unit_value_assessed=it.unit_value_assessed,
            total_value_declared_usd=it.total_value_declared_usd,
            total_value_assessed_usd=it.total_value_assessed_usd,
            custom_value_declared_pkr=it.custom_value_declared_pkr,
            custom_value_assessed_pkr=it.custom_value_assessed_pkr,
        ))
        count += 1
    db.flush()
    return count


def find_duplicate_gd_number_for_extract(
    entry_id: int,
    gd_number: Optional[str],
    db: Session,
) -> Optional[ExBondEntry]:
    """Check duplicate EB GD number before validate, using extracted gd_number."""
    if not gd_number or not str(gd_number).strip():
        return None
    entry = get_entry_or_error(entry_id, db)
    norm = str(gd_number).strip().upper()
    return (db.query(ExBondEntry)
              .filter(ExBondEntry.into_bond_gd_id == entry.into_bond_gd_id,
                      ExBondEntry.entry_id != entry.entry_id,
                      func.upper(func.btrim(ExBondEntry.gd_number)) == norm)
              .first())


def find_duplicate_gd_number(entry: ExBondEntry, db: Session) -> Optional[ExBondEntry]:
    """Another Partial GD on the same Into-Bond GD already carrying this EB GD Number
    (case/whitespace-insensitive), if any. None when this entry has no gd_number yet."""
    if not entry.gd_number or not entry.gd_number.strip():
        return None
    norm = entry.gd_number.strip().upper()
    return (db.query(ExBondEntry)
              .filter(ExBondEntry.into_bond_gd_id == entry.into_bond_gd_id,
                      ExBondEntry.entry_id != entry.entry_id,
                      func.upper(func.btrim(ExBondEntry.gd_number)) == norm)
              .first())


# ---------------------------------------------------------------------------
# Validate against the selected Quota Approval + finalize
# ---------------------------------------------------------------------------
def commit_staged_entry_attachment(
    entry: ExBondEntry,
    kind: str,
    staged_file: str,
    original_filename: str,
    db: Session,
    user_id: int,
    *,
    replace: bool = True,
) -> GDAttachment:
    from utils.staging import staged_dir
    from modules.weboc.gd_service import ATTACH_STAGE_SUBDIR, attach_upload_dir

    attach_dir = attach_upload_dir()
    stage_path = os.path.join(
        staged_dir(ATTACH_STAGE_SUBDIR, attach_dir), os.path.basename(staged_file))
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
        return replace_entry_attachment(entry, kind, orig, contents, db, user_id)
    return add_entry_attachment(entry, kind, orig, contents, db, user_id)


def validate_partial_gd_approval(entry_id: int, data: PartialGdValidateApproval,
                                  user_id: int, db: Session) -> ExBondEntry:
    entry = get_entry_or_error(entry_id, db)
    gd = get_gd_or_error(entry.into_bond_gd_id, db)

    if data.staged_view_file:
        commit_staged_entry_attachment(
            entry, "EX_BOND_GD_VIEW", data.staged_view_file,
            data.original_view_filename or "gd_view.pdf", db, user_id,
        )
    if data.view:
        apply_partial_gd_view(entry, data.view)

    for pending in data.pending_item_uploads or []:
        att = commit_staged_entry_attachment(
            entry, "EX_BOND_ITEM_DETAILS", pending.staged_file,
            pending.original_filename or "item_details.pdf", db, user_id, replace=False,
        )
        apply_partial_gd_item_details(entry, pending.items or [], att.attachment_id, db)

    if data.quantity_mt is not None:
        entry.quantity_mt = data.quantity_mt

    items = (db.query(ExBondItem).filter(ExBondItem.entry_id == entry.entry_id)
               .order_by(ExBondItem.item_number).all())
    if not items:
        raise ValidationError(
            "Upload at least one Item Details document for this Partial GD before validating."
        )

    # Prevent the same physical EB document being recorded as two separate releases
    # against the same Into-Bond GD (would double-count against the bonded quantity).
    # No override — this is a data-integrity rule, not a quantity judgment call.
    dup = find_duplicate_gd_number(entry, db)
    if dup:
        raise ValidationError(
            f"EB GD Number '{entry.gd_number}' has already been recorded against this "
            f"Into-Bond GD as Partial GD #{dup.entry_id} "
            f"({'finalized' if dup.is_finalized else 'draft'}). "
            f"Duplicate Ex-Bond releases are not allowed."
        )

    approval = db.query(EdbApproval).filter(EdbApproval.approval_id == data.approval_id).first()
    if not approval:
        raise NotFoundError("Quota approval not found")
    groups = db.query(SroGroupNumber).filter(SroGroupNumber.approval_id == approval.approval_id).all()
    approval_refs = _approval_refs(approval, groups)

    # SRO Number must always be extracted from THIS Partial GD's own Item Details, and
    # validated against the selected Quota Approval Number. Any mismatch (or missing SRO
    # reference) hard-blocks the release — no force/override, per the confirmed rule.
    mismatches = []
    matched_refs = []
    for it in items:
        item_refs = _refs_in(it.sro_no) | _refs_in(it.quota_reference)
        label = it.sro_no or it.quota_reference
        if not item_refs:
            mismatches.append(f"item {it.item_number or '?'} has no SRO/quota reference")
            continue
        if not (item_refs & approval_refs):
            mismatches.append(
                f"item {it.item_number or '?'} SRO reference '{label}' does not match "
                f"the selected quota approval '{approval.main_sro_no or approval.approval_no}'"
            )
            continue
        matched_refs.append(label)

    if mismatches:
        raise ValidationError(
            "SRO / approval number mismatch — release blocked: " + "; ".join(mismatches) + "."
        )

    qty = data.quantity_mt
    if qty is None or qty <= 0:
        summed = sum((_entry_item_qty_mt(it) or Decimal("0")) for it in items)
        qty = summed if summed > 0 else None
    if qty is None or qty <= 0:
        qty = entry.quantity_mt
    if qty is None or qty <= 0:
        raise ValidationError("Quantity lifted (MT) is required.")

    # Bonded-quantity / remaining-balance check — unchanged, still force-overridable.
    exceeds, remaining, over_by = ex_bond_would_exceed(gd, db, qty, exclude_entry_id=entry.entry_id)
    if exceeds and not data.force:
        raise ConflictError(
            f"This lifting of {float(qty):,.3f} MT exceeds the bonded quantity by "
            f"{over_by:,.3f} MT (only {remaining:,.3f} MT remains). "
            f"Correct the quantity, or confirm to record it anyway."
        )

    entry.approval_id = approval.approval_id
    entry.matched_sro_no = " | ".join(sorted({r for r in matched_refs if r})) or None
    entry.quantity_mt = qty
    entry.is_finalized = True
    try:
        db.flush()
        recompute_gd_status(gd, db)
        scan_bond_alerts(db, [gd.gd_id])
        db.commit()
    except IntegrityError:
        # Safety net for the ux_ex_bond_entries_ib_gd_number unique index — catches a
        # concurrent request finalizing the same EB GD Number between our check above
        # and this commit. The app-level check handles the common case with a friendlier
        # message; this guarantees correctness under a race either way.
        db.rollback()
        raise ConflictError(
            f"EB GD Number '{entry.gd_number}' was just recorded against this Into-Bond "
            f"GD by another request. Duplicate Ex-Bond releases are not allowed."
        )
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Read / delete
# ---------------------------------------------------------------------------
def get_partial_gd_detail(entry_id: int, db: Session) -> dict:
    entry = get_entry_or_error(entry_id, db)
    atts = (db.query(GDAttachment).filter(GDAttachment.ex_bond_entry_id == entry.entry_id)
              .order_by(GDAttachment.attachment_id).all())
    items = (db.query(ExBondItem).filter(ExBondItem.entry_id == entry.entry_id)
               .order_by(ExBondItem.item_number).all())
    gd_view_att = next((a for a in atts if a.kind == "EX_BOND_GD_VIEW"), None)
    item_atts = [a for a in atts if a.kind == "EX_BOND_ITEM_DETAILS"]

    return {
        "entry_id": entry.entry_id,
        "into_bond_gd_id": entry.into_bond_gd_id,
        "gd_number": entry.gd_number,
        "ex_bond_date": entry.ex_bond_date.isoformat() if entry.ex_bond_date else None,
        "quantity_mt": _f(entry.quantity_mt),
        "approval_id": entry.approval_id,
        "matched_sro_no": entry.matched_sro_no,
        "is_finalized": bool(entry.is_finalized),
        "remarks": entry.remarks,
        "gd_view": {
            "uploaded": gd_view_att is not None,
            "attachment_id": gd_view_att.attachment_id if gd_view_att else None,
            "filename": gd_view_att.filename if gd_view_att else None,
        },
        "item_details": [{
            "attachment_id": a.attachment_id, "filename": a.filename,
            "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
        } for a in item_atts],
        "items": [{
            "item_id": it.item_id, "item_number": it.item_number, "hs_code": it.hs_code,
            "goods_description": it.goods_description, "quantity": _f(it.quantity),
            "unit": it.unit, "sro_no": it.sro_no, "quota_reference": it.quota_reference,
            "m_size": it.m_size,
            "source_attachment_id": it.source_attachment_id,
        } for it in items],
    }


def delete_partial_gd_item_details_doc(entry_id: int, attachment_id: int, db: Session,
                                        deleted_by: int) -> ExBondEntry:
    """Remove one Item Details document from a Partial GD: its attachment and every
    ExBondItem row it produced are deleted together in one commit (never leaves orphaned
    item rows pointing at a gone document). If the entry had already been finalized, its
    approval validation no longer reflects the remaining items, so it's reset to draft
    and must be re-validated before it can be recorded again."""
    entry = get_entry_or_error(entry_id, db)
    att = (db.query(GDAttachment)
             .filter(GDAttachment.attachment_id == attachment_id,
                     GDAttachment.ex_bond_entry_id == entry.entry_id,
                     GDAttachment.kind == "EX_BOND_ITEM_DETAILS")
             .first())
    if not att:
        raise NotFoundError("Item Details document not found on this Partial GD")

    removed = (db.query(ExBondItem)
                 .filter(ExBondItem.source_attachment_id == attachment_id).count())

    log_audit(db, deleted_by, "DELETE_PARTIAL_GD_ITEM_DETAILS", entity_type="GD_ATTACHMENT",
              entity_id=att.attachment_id,
              old_value={"entry_id": entry.entry_id, "filename": att.filename, "items_removed": removed},
              description=f"Deleted Item Details document {att.attachment_id} from Partial GD "
                          f"{entry.entry_id} ({removed} item(s) removed)")

    # Atomic: the attachment and every item it produced are removed in this one commit.
    db.query(ExBondItem).filter(ExBondItem.source_attachment_id == attachment_id) \
        .delete(synchronize_session=False)
    if att.file_path and os.path.exists(att.file_path):
        try:
            os.remove(att.file_path)
        except OSError:
            pass
    db.delete(att)

    if entry.is_finalized:
        entry.is_finalized = False
        entry.approval_id = None
        entry.matched_sro_no = None

    db.commit()

    gd = get_gd_or_error(entry.into_bond_gd_id, db)
    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    db.commit()
    db.refresh(entry)
    return entry


def delete_partial_gd(entry_id: int, db: Session, deleted_by: int) -> GoodsDeclaration:
    entry = get_entry_or_error(entry_id, db)
    gd_id = entry.into_bond_gd_id
    atts = db.query(GDAttachment).filter(GDAttachment.ex_bond_entry_id == entry.entry_id).all()

    log_audit(db, deleted_by, "DELETE_PARTIAL_GD", entity_type="EX_BOND_ENTRY", entity_id=entry.entry_id,
              old_value={"into_bond_gd_id": gd_id, "gd_number": entry.gd_number},
              description=f"Deleted Partial GD {entry.entry_id} (into-bond GD {gd_id})")

    for a in atts:
        if a.file_path and os.path.exists(a.file_path):
            try:
                os.remove(a.file_path)
            except OSError:
                pass
        db.delete(a)
    db.delete(entry)  # ex_bond_items cascade via the ORM relationship + DB FK
    db.commit()

    gd = get_gd_or_error(gd_id, db)
    recompute_gd_status(gd, db)
    scan_bond_alerts(db, [gd.gd_id])
    db.commit()
    return gd
