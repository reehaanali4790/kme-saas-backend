"""SRO / EDB Quota business logic - approval CRUD, group SRO numbers, quota items,
usage/report assembly. Extracted from modules/weboc/sro_router.py.
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from core.exceptions import ConflictError, NotFoundError, ValidationError
from models.database_models import EdbApproval, GoodsDeclaration, SroGroupNumber, SroQuotaItem
from modules.weboc.helpers.sro_usage import compute_usage
from modules.weboc.sro_schemas import ApprovalSave, SroGroupNumberIn, SroQuotaItemSave
from infrastructure.normalization.normalization_service import (
    normalize_importer, company_resolver, matches_company_code,
)


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def consumed(item: SroQuotaItem) -> Decimal:
    """Quantity an item consumes from its approval - assessed if set, else declared, else 0."""
    q = item.assessed_qty_mt if item.assessed_qty_mt is not None else item.declared_qty_mt
    return Decimal(q) if q is not None else Decimal("0")


def _approval_consumed(approval_id: int, db: Session) -> Decimal:
    """Total consumed against an approval across all GDs (manual quota lines only - the
    full picture incl. Item-Details matches comes from compute_usage)."""
    total = Decimal("0")
    items = db.query(SroQuotaItem).filter(SroQuotaItem.approval_id == approval_id).all()
    for it in items:
        total += consumed(it)
    return total


def validity(a: EdbApproval, today: Optional[date] = None) -> str:
    """Active / Expired / Not started - by the quota's validity window."""
    today = today or date.today()
    if a.end_date and a.end_date < today:
        return "Expired"
    if a.start_date and a.start_date > today:
        return "Not started"
    return "Active"


def approval_to_dict(a: EdbApproval, db: Session, usage: Optional[dict] = None) -> dict:
    approved = Decimal(a.approved_qty_mt) if a.approved_qty_mt is not None else None
    if usage is not None:
        used = Decimal(str(usage.get(a.approval_id, {}).get("used_qty_mt") or 0))
    else:
        used = _approval_consumed(a.approval_id, db)
    remaining = (approved - used) if approved is not None else None
    sro_val = a.main_sro_no or a.approval_no
    return {
        "approval_id": a.approval_id,
        "approval_no": a.approval_no,
        "company_name": a.company_name,
        "main_sro_no": sro_val,
        "sro_no": sro_val,
        "sro_number": sro_val,
        "group_sro_numbers": [{"group_id": g.group_id, "group_sro_no": g.group_sro_no,
                               "hs_code": g.hs_code}
                              for g in sorted(a.group_numbers, key=lambda x: x.group_id)],
        "hs_code": a.hs_code,
        "part_name": a.part_name,
        "use_type": a.use_type,
        "unit": a.unit,
        "start_date": a.start_date.isoformat() if a.start_date else None,
        "end_date": a.end_date.isoformat() if a.end_date else None,
        "short_description": a.short_description,
        "notes": a.notes,
        "status": validity(a),
        "approved_qty_mt": _f(approved),
        "consumed_qty_mt": _f(used),
        "remaining_qty_mt": _f(remaining),
    }


def item_to_dict(it: SroQuotaItem) -> dict:
    return {
        "item_id": it.item_id,
        "gd_id": it.gd_id,
        "approval_id": it.approval_id,
        "approval_no": it.approval.approval_no if it.approval else None,
        "hs_code": it.hs_code,
        "part_name": it.part_name,
        "declared_qty_mt": _f(it.declared_qty_mt),
        "assessed_qty_mt": _f(it.assessed_qty_mt),
        "consumed_qty_mt": _f(consumed(it)),
    }


def apply_groups(a: EdbApproval, rows: Optional[List[SroGroupNumberIn]], db: Session) -> None:
    """Rewrite the approval's group (child) SRO numbers - several per main SRO."""
    if rows is None:
        return
    a.group_numbers.clear()
    db.flush()
    seen = set()
    for row in rows:
        if not row.group_sro_no or row.group_sro_no in seen:
            continue
        seen.add(row.group_sro_no)
        a.group_numbers.append(SroGroupNumber(group_sro_no=row.group_sro_no, hs_code=row.hs_code))


def apply_approval_fields(a: EdbApproval, data: ApprovalSave, db: Session) -> None:
    """Every field applies whenever the key is PRESENT in the payload, even to clear it
    to None (matches the original _apply_approval() exactly - unlike gd_schemas.py, there
    is no "only if truthy" group here)."""
    fields_set = data.model_fields_set
    for field in ("approval_no", "company_name", "main_sro_no", "hs_code", "part_name",
                  "use_type", "unit", "short_description", "notes",
                  "start_date", "end_date", "approved_qty_mt"):
        if field in fields_set:
            setattr(a, field, getattr(data, field))
    if not a.main_sro_no:
        if "sro_no" in fields_set and data.sro_no:
            a.main_sro_no = data.sro_no
        elif "sro_number" in fields_set and data.sro_number:
            a.main_sro_no = data.sro_number
    if "company_name" in fields_set and a.company_name:
        canon = normalize_importer(a.company_name, db)
        if canon:
            a.company_name = canon
    if not a.unit:
        a.unit = "MT"
    # Keep the legacy approval_no populated so older screens/records still resolve.
    if not a.approval_no and a.main_sro_no:
        a.approval_no = a.main_sro_no
    if "group_sro_numbers" in fields_set:
        apply_groups(a, data.group_sro_numbers, db)


def validate_approval(a: EdbApproval) -> None:
    if not a.main_sro_no:
        raise ValidationError("Main SRO Number is required.")
    if not a.company_name:
        raise ValidationError("Company Name is required.")
    if a.start_date and a.end_date and a.end_date < a.start_date:
        raise ValidationError("End Date cannot be before Start Date.")
    if a.approved_qty_mt is not None and a.approved_qty_mt < 0:
        raise ValidationError("Quantity in MT cannot be negative.")


def get_approval_or_404(approval_id: int, db: Session) -> EdbApproval:
    a = db.query(EdbApproval).filter(EdbApproval.approval_id == approval_id).first()
    if not a:
        raise NotFoundError("Approval not found")
    return a


def list_approvals(db: Session) -> List[EdbApproval]:
    return (db.query(EdbApproval).options(joinedload(EdbApproval.group_numbers))
              .order_by(EdbApproval.approval_id.desc()).all())


def create_approval(data: ApprovalSave, db: Session, created_by: int) -> EdbApproval:
    a = EdbApproval(created_by=created_by)
    apply_approval_fields(a, data, db)
    validate_approval(a)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def update_approval(approval_id: int, data: ApprovalSave, db: Session) -> EdbApproval:
    a = get_approval_or_404(approval_id, db)
    apply_approval_fields(a, data, db)
    validate_approval(a)
    db.commit()
    db.refresh(a)
    return a


def delete_approval(approval_id: int, db: Session) -> None:
    a = get_approval_or_404(approval_id, db)
    in_use = db.query(func.count(SroQuotaItem.item_id)).filter(
        SroQuotaItem.approval_id == approval_id).scalar()
    if in_use:
        raise ConflictError(f"Approval is used by {in_use} quota item(s). Remove them first.")
    db.delete(a)
    db.commit()


def quota_report(db: Session, company: Optional[list[str]], main_sro_no: Optional[list[str]],
                  hs_code: Optional[list[str]], status: str) -> dict:
    """Approved vs used vs remaining per SRO quota, with the consuming GD lines.

    Used quantity comes from GD data (manual quota lines + Item-Details items auto-matched
    by company + SRO/HS). Items whose match is unclear are NOT counted - they are returned
    in `unclear` so the import team can link them by hand."""
    q = db.query(EdbApproval).options(joinedload(EdbApproval.group_numbers))
    approvals = q.order_by(EdbApproval.company_name, EdbApproval.main_sro_no).all()
    cr = company_resolver(db)
    def _normalize_list(v) -> list[str]:
        if not v:
            return []
        if isinstance(v, str):
            v = [v]
        res = []
        for item in v:
            if item:
                for part in str(item).split(","):
                    p = part.strip()
                    if p:
                        res.append(p)
        return res

    company = _normalize_list(company)
    main_sro_no = _normalize_list(main_sro_no)
    hs_code = _normalize_list(hs_code)

    if company:
        approvals = [a for a in approvals if any(matches_company_code(cr, a.company_name, c) for c in company)]
    if main_sro_no:
        approvals = [a for a in approvals if a.main_sro_no in main_sro_no]
    if hs_code:
        approvals = [a for a in approvals if a.hs_code in hs_code]

    usage = compute_usage(db)
    rows = []
    for a in approvals:
        d = approval_to_dict(a, db, usage)
        if (status or "All") != "All" and d["status"] != status:
            continue
        approved = d["approved_qty_mt"]
        used = d["consumed_qty_mt"] or 0
        d["usage_pct"] = round(used / approved * 100, 1) if approved else None
        d["over_quota"] = approved is not None and used > approved
        if d["over_quota"]:
            d["quota_band"] = "exceeded"
        elif d["usage_pct"] is not None and d["usage_pct"] >= 90:
            d["quota_band"] = "90"
        elif d["usage_pct"] is not None and d["usage_pct"] >= 80:
            d["quota_band"] = "80"
        else:
            d["quota_band"] = None
        d["usage_lines"] = usage.get(a.approval_id, {}).get("lines", [])
        res = cr.resolve(d.get("company_name"))
        d["company_code"] = res.get("short_code")
        d["company_matched"] = res.get("matched")
        rows.append(d)

    totals = {
        "approved_qty_mt": round(sum(r["approved_qty_mt"] or 0 for r in rows), 3),
        "used_qty_mt": round(sum(r["consumed_qty_mt"] or 0 for r in rows), 3),
    }
    totals["remaining_qty_mt"] = round(totals["approved_qty_mt"] - totals["used_qty_mt"], 3)

    warnings = []
    for r in rows:
        name = r["main_sro_no"] or r["approval_no"] or f"#{r['approval_id']}"
        if r["over_quota"]:
            over = (r["consumed_qty_mt"] or 0) - (r["approved_qty_mt"] or 0)
            warnings.append(f"SRO quota exceeded — {name} ({r['company_name'] or 'no company'}): "
                            f"used {r['consumed_qty_mt']:,.3f} MT of {r['approved_qty_mt']:,.3f} MT "
                            f"approved (over by {over:,.3f} MT).")
        elif r.get("quota_band") == "90":
            warnings.append(f"SRO quota at 90% — {name} ({r['company_name'] or 'no company'}): "
                            f"{r['usage_pct']}% used.")
        elif r.get("quota_band") == "80":
            warnings.append(f"SRO quota at 80% — {name} ({r['company_name'] or 'no company'}): "
                            f"{r['usage_pct']}% used.")
        if r["status"] == "Expired":
            warnings.append(f"SRO quota expired — {name} ({r['company_name'] or 'no company'}) "
                            f"ended {r['end_date']}.")

    return {
        "count": len(rows), "rows": rows, "totals": totals,
        "warnings": warnings,
        "unclear": usage.get("_unclear", []),
        "notes": ("Used quantity = manually linked GD quota lines + WeBOC Item Details items "
                  "auto-matched to a quota by company and SRO/HS code. Items with a low-confidence "
                  "match are listed separately and are NOT counted in Used."),
    }


def report_options(db: Session) -> dict:
    rows = db.query(EdbApproval).all()
    cr = company_resolver(db)
    
    company_codes: set[str] = set()
    mains_set: set[str] = set()
    hs_set: set[str] = set()

    for a in rows:
        if a.company_name:
            code = cr.resolve(a.company_name).get("short_code")
            if code:
                company_codes.add(str(code))
        main_val = a.main_sro_no or a.approval_no
        if main_val:
            mains_set.add(str(main_val))
        if a.hs_code:
            hs_set.add(str(a.hs_code))

    companies = sorted(company_codes)
    mains = sorted(mains_set)
    hs = sorted(hs_set)
    return {"companies": companies, "main_sro_numbers": mains, "hs_codes": hs}


# ---------------------------------------------------------------------------
# Quota items (per GD)
# ---------------------------------------------------------------------------

def list_gd_items(gd_id: int, db: Session) -> List[SroQuotaItem]:
    return (db.query(SroQuotaItem).filter(SroQuotaItem.gd_id == gd_id)
              .order_by(SroQuotaItem.item_id).all())


def list_gd_items_for_approval(approval_id: int, db: Session) -> List[SroQuotaItem]:
    """The manual quota lines drawing against one approval (shown on its detail page)."""
    return db.query(SroQuotaItem).filter(SroQuotaItem.approval_id == approval_id).all()


def get_item_or_404(item_id: int, db: Session) -> SroQuotaItem:
    it = db.query(SroQuotaItem).filter(SroQuotaItem.item_id == item_id).first()
    if not it:
        raise NotFoundError("Quota item not found")
    return it


def add_gd_item(gd_id: int, data: SroQuotaItemSave, db: Session) -> SroQuotaItem:
    gd = db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).first()
    if not gd:
        raise NotFoundError("GD not found")
    if data.approval_id is not None:
        exists = db.query(EdbApproval).filter(EdbApproval.approval_id == data.approval_id).first()
        if not exists:
            raise NotFoundError("Approval not found")
    it = SroQuotaItem(
        gd_id=gd_id, approval_id=data.approval_id, hs_code=data.hs_code,
        part_name=data.part_name, declared_qty_mt=data.declared_qty_mt,
        assessed_qty_mt=data.assessed_qty_mt,
    )
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


def update_item(item_id: int, data: SroQuotaItemSave, db: Session) -> SroQuotaItem:
    it = get_item_or_404(item_id, db)
    fields_set = data.model_fields_set
    if "approval_id" in fields_set:
        if data.approval_id is not None and not db.query(EdbApproval).filter(
                EdbApproval.approval_id == data.approval_id).first():
            raise NotFoundError("Approval not found")
        it.approval_id = data.approval_id
    for field in ("hs_code", "part_name", "declared_qty_mt", "assessed_qty_mt"):
        if field in fields_set:
            setattr(it, field, getattr(data, field))
    db.commit()
    db.refresh(it)
    return it


def delete_item(item_id: int, db: Session) -> None:
    it = get_item_or_404(item_id, db)
    db.delete(it)
    db.commit()
