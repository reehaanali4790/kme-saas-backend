"""
Bank Limit Router — API endpoints for Bank Limits
"""
import logging
from collections import defaultdict
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

from core.tenant import get_tenant_db
from models.database_models import BankLimit, User
from modules.auth.dependencies import get_current_user
from core.exceptions import ValidationError
from core.permissions import require_min_role
from infrastructure.normalization.normalization_service import company_key, company_resolver, matches_company_code
from utils.parsing import parse_date, parse_float
from . import services as svc
from .schemas import (
    BANK_LIMIT_TYPE_LABEL,
    BankLimitCreate,
    BankLimitListOut,
    BankLimitOptionsOut,
    BankLimitOut,
    BankLimitUpdate,
    SaveResult,
)

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/bank-limits", tags=["Bank Limits"])

_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get("/", response_model=BankLimitListOut)
def list_limits(bank: str = Query(None), group_company: str = Query(None),
                 db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    rows = svc.list_limits(db, bank, group_company)
    return BankLimitListOut(count=len(rows), items=[svc.limit_to_schema(r) for r in rows])


@router.get("/options", response_model=BankLimitOptionsOut)
def limit_options(db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(get_current_user)):
    return BankLimitOptionsOut(**svc.get_limit_options(db))


@router.post("/", response_model=SaveResult)
def create_limit(data: BankLimitCreate, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(_can_write)):
    bl, warnings = svc.create_limit(db, data, created_by=current_user.user_id)
    logger.info(f"BankLimit created id={bl.limit_id} bank={bl.bank_name} "
                f"group={bl.group_company} type={bl.bank_limit_type} lc={bl.lc_type} "
                f"rev={bl.revision_no}")
    return SaveResult(limit_id=bl.limit_id, revision_no=bl.revision_no, warnings=warnings)


@router.get("/{limit_id:int}", response_model=BankLimitOut)
def get_limit(limit_id: int, db: Session = Depends(get_tenant_db),
              current_user: User = Depends(get_current_user)):
    return svc.limit_to_schema(svc.get_limit_or_404(db, limit_id))


@router.put("/{limit_id:int}", response_model=SaveResult)
def update_limit(limit_id: int, data: BankLimitUpdate, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(_can_write)):
    bl, warnings = svc.update_limit(db, limit_id, data, updated_by=current_user.user_id)
    return SaveResult(limit_id=limit_id, warnings=warnings)


@router.delete("/{limit_id:int}")
def delete_limit(limit_id: int, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(_can_write)):
    user_id = current_user.user_id
    svc.delete_limit(db, limit_id, deleted_by=user_id)
    return {"success": True}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@router.get("/report/options")
def report_options(db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(get_current_user)):
    return limit_options(db, current_user)


@router.get("/report")
def bank_limit_report(date_from: str = Query(...), date_to: str = Query(...),
                       bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                       branch: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                       group_company: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                       company: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                       bank_limit_type: str = Query("All"), lc_type: str = Query("All"),
                       status: str = Query("All"), revision_id: int = Query(None),
                       db: Session = Depends(get_tenant_db),
                       current_user: User = Depends(get_current_user)):
    df, dt = parse_date(date_from), parse_date(date_to)
    if not df or not dt:
        raise ValidationError("From Date and To Date are required.")
    if dt < df:
        raise ValidationError("To Date cannot be before From Date.")

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

    bank_list = _normalize_list(bank)
    branch_list = _normalize_list(branch)
    group_list = _normalize_list(group_company)
    company_list = _normalize_list(company)

    cr = company_resolver(db)

    q = db.query(BankLimit).options(joinedload(BankLimit.lines)).filter(
        BankLimit.valid_from <= dt, BankLimit.valid_to >= df)
    if bank_list:
        q = q.filter(BankLimit.bank_name.in_(bank_list))
    if branch_list:
        q = q.filter(BankLimit.branch.in_(branch_list))
    if bank_limit_type and bank_limit_type != "All":
        q = q.filter(BankLimit.bank_limit_type == bank_limit_type.upper())
    if lc_type and lc_type != "All":
        q = q.filter(BankLimit.lc_type == lc_type.upper())
    revs = q.all()
    if group_list:
        revs = [r for r in revs if any(matches_company_code(cr, r.group_company, gc) for gc in group_list)]

    if (status or "All") != "All":
        revs = [r for r in revs if svc.validity_status(r) == status]

    ctx_map = defaultdict(list)
    for r in revs:
        ctx_map[(r.bank_name, r.branch or "", r.group_company,
                 (r.bank_limit_type or "REGULAR"))].append(r)

    # One query for the whole report, grouped by bank, instead of re-running the same
    # expensive "load every LC in range" query once per (bank, branch, group, type) context.
    bank_lcs_map = svc.open_lcs_by_bank_all(db, df, dt)

    contexts = []
    for (bnk, brn, grp, blt), rlist in sorted(ctx_map.items()):
        rlist.sort(key=lambda x: (x.valid_from, x.revision_no))
        overlapping = [{
            "limit_id": r.limit_id, "revision_no": r.revision_no,
            "valid_from": r.valid_from.isoformat(), "valid_to": r.valid_to.isoformat(),
            "group_limit_amount": parse_float(r.group_limit_amount),
            "lc_type": svc.norm_lc_type(r.lc_type),
            "validity_status": svc.validity_status(r),
        } for r in rlist]

        active = None
        if revision_id:
            active = next((r for r in rlist if r.limit_id == revision_id), None)
        if not active:
            active = max(rlist, key=lambda x: (x.valid_from, x.revision_no))
        limit_lc = svc.norm_lc_type(active.lc_type)

        parent_line = next((ln for ln in active.lines if svc.is_parent(ln)), None)
        parent_name = parent_line.company_name if parent_line else grp
        parent_key = company_key(parent_name)
        child_lines = [ln for ln in active.lines if not svc.is_parent(ln)]
        child_by_key = {company_key(ln.company_name): ln for ln in child_lines}

        group_keys = svc.group_company_keys(active)
        parent_code = cr.resolve(parent_name).get("short_code")
        parent_canon = (cr.resolve(parent_name).get("canonical") or "").upper()

        open_lcs = [
            lc for lc in bank_lcs_map.get(bnk, [])
            if svc.lc_in_group(lc, group_keys)
            or (parent_code and cr.resolve(lc.importer_name).get("short_code") == parent_code)
            or any(cr.resolve(lc.importer_name).get("short_code") == cr.resolve(ln.company_name).get("short_code") for ln in child_lines)
        ]
        util_by_line = defaultdict(float)
        miss_by_line = defaultdict(int)
        lcs_by_line = defaultdict(list)
        parent_used = 0.0
        parent_missing = 0
        parent_lcs = []
        group_used = 0.0
        unattributed = 0.0
        unattributed_missing = 0
        unattributed_lcs = []
        for lc in open_lcs:
            tenor = svc.lc_tenor(lc)
            if not svc.type_applies(limit_lc, tenor):
                continue
            row = svc.lc_row(lc, brn or None)
            ck = company_key(lc.importer_name)
            lc_res = cr.resolve(lc.importer_name)
            lc_code = lc_res.get("short_code")
            lc_canon = (lc_res.get("canonical") or "").upper()

            child = child_by_key.get(ck)
            if not child and child_lines:
                for ln in child_lines:
                    ln_res = cr.resolve(ln.company_name)
                    ln_code = ln_res.get("short_code")
                    ln_canon = (ln_res.get("canonical") or "").upper()
                    if (
                        company_key(ln.company_name) == ck
                        or (lc_code and ln_code and lc_code == ln_code)
                        or (lc_canon and ln_canon and lc_canon == ln_canon)
                    ):
                        child = ln
                        break

            is_parent_match = (
                (not child_lines)
                or (ck == parent_key)
                or (lc_code and parent_code and lc_code == parent_code)
                or (lc_canon and parent_canon and lc_canon == parent_canon)
            )

            if row["missing_rate"]:
                if child and svc.type_applies(child.lc_type, tenor):
                    miss_by_line[child.line_id] += 1
                    lcs_by_line[child.line_id].append(svc.lc_brief(row))
                elif is_parent_match:
                    parent_missing += 1
                    parent_lcs.append(svc.lc_brief(row))
                else:
                    unattributed_missing += 1
                    unattributed_lcs.append(svc.lc_brief(row))
                continue
            pkr = row["lc_amount_pkr"] or 0.0
            group_used += pkr
            if child and svc.type_applies(child.lc_type, tenor):
                util_by_line[child.line_id] += pkr
                lcs_by_line[child.line_id].append(svc.lc_brief(row))
            elif is_parent_match:
                parent_used += pkr
                parent_lcs.append(svc.lc_brief(row))
            else:
                unattributed += pkr
                unattributed_lcs.append(svc.lc_brief(row))
        for lst in list(lcs_by_line.values()) + [parent_lcs, unattributed_lcs]:
            lst.sort(key=lambda r: (r["lc_date"] or "", r["lc_number"] or ""))

        group_offered = parse_float(active.group_limit_amount) or 0.0
        group_used = round(group_used, 2)
        group_available = round(group_offered - group_used, 2)

        q_from = active.valid_from.isoformat()
        q_to = active.valid_to.isoformat()

        rows = []
        # Parent row first: may use the full available group limit.
        if not company_list or any(matches_company_code(cr, parent_name, c) for c in company_list):
            rows.append({
                "line_id": parent_line.line_id if parent_line else None,
                "company_name": cr.resolve(parent_name).get("short_code") or parent_name,
                "company_name_raw": parent_name,
                "limit_type": "PARENT",
                "lc_type": svc.norm_lc_type(parent_line.lc_type) if parent_line else limit_lc,
                "offered": group_offered, "utilized": round(parent_used, 2),
                "available": group_available,
                "utilization_pct": round(parent_used / group_offered * 100, 1) if group_offered else None,
                "missing_rate_count": parent_missing,
                "quota_from": q_from, "quota_to": q_to,
                "lcs": parent_lcs,
            })
        # Child rows: allowed portion of the group; available capped by the group available.
        for ln in sorted(child_lines, key=lambda x: x.line_id):
            if company_list and not any(matches_company_code(cr, ln.company_name, c) for c in company_list):
                continue
            offered = parse_float(ln.sub_limit_amount) or 0.0
            used = round(util_by_line.get(ln.line_id, 0.0), 2)
            child_avail = round(min(offered - used, group_available), 2)
            rows.append({
                "line_id": ln.line_id,
                "company_name": cr.resolve(ln.company_name).get("short_code") or ln.company_name,
                "company_name_raw": ln.company_name,
                "limit_type": "CHILD", "lc_type": svc.norm_lc_type(ln.lc_type),
                "offered": offered, "utilized": used, "available": child_avail,
                "utilization_pct": round(used / offered * 100, 1) if offered else None,
                "missing_rate_count": miss_by_line.get(ln.line_id, 0),
                "quota_from": q_from, "quota_to": q_to,
                "lcs": lcs_by_line.get(ln.line_id, []),
            })

        total_missing = (sum(miss_by_line.values()) + parent_missing + unattributed_missing)
        vstatus = svc.validity_status(active)

        temp_warning = None
        if blt == "TEMPORARY" and vstatus == "Expired" and group_used > 0:
            temp_warning = ("This Temporary limit has expired but still has open exposure "
                             f"(PKR {group_used:,.0f}). It no longer increases the available limit.")

        contexts.append({
            "bank_name": bnk, "branch": brn or None, "group_company": grp,
            "bank_limit_type": blt,
            "bank_limit_type_label": BANK_LIMIT_TYPE_LABEL.get(blt, blt),
            "lc_type": limit_lc,
            "active_revision": {
                "limit_id": active.limit_id, "revision_no": active.revision_no,
                "valid_from": active.valid_from.isoformat(),
                "valid_to": active.valid_to.isoformat(),
                "remarks": active.remarks,
            },
            "overlapping_revisions": overlapping,
            "multiple_revisions": len(overlapping) > 1,
            "cards": {
                "group_limit": group_offered, "utilized": group_used,
                "available": group_available,
                "utilization_pct": round(group_used / group_offered * 100, 1) if group_offered else None,
                "validity_status": vstatus,
                "lc_type": limit_lc,
                "bank_limit_type": blt,
                "bank_limit_type_label": BANK_LIMIT_TYPE_LABEL.get(blt, blt),
            },
            "rows": rows,
            "totals": {"offered": group_offered, "utilized": group_used,
                       "available": group_available, "missing_rate_count": total_missing},
            "unattributed_utilized": round(unattributed, 2),
            "unattributed_missing_rate": unattributed_missing,
            "unattributed_lcs": unattributed_lcs,
            "temp_warning": temp_warning,
        })

    return {"date_from": df.isoformat(), "date_to": dt.isoformat(),
            "count": len(contexts), "contexts": contexts}


@router.get("/report/drilldown")
def report_drilldown(date_from: str = Query(...), date_to: str = Query(...),
                      bank: str = Query(...), company: str = Query(None),
                      branch: str = Query(None), lc_type: str = Query(None),
                      group_company: str = Query(None), limit_id: int = Query(None),
                      db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(get_current_user)):
    """The utilized-LC list behind a bank(+company) figure — for the import team to verify.
    lc_type (SIGHT/DA/BOTH) mirrors the limit so the list matches the reported figure.
    limit_id / group_company scope utilization to one parent group (no cross-group mixing)."""
    df, dt = parse_date(date_from), parse_date(date_to)
    if not df or not dt:
        raise ValidationError("From Date and To Date are required.")
    cr = company_resolver(db)
    want_lc = svc.norm_lc_type(lc_type) if (lc_type and lc_type not in ("All", "")) else "BOTH"

    group_keys = None
    if limit_id:
        bl = db.query(BankLimit).options(joinedload(BankLimit.lines)).filter(
            BankLimit.limit_id == limit_id).first()
        if bl:
            group_keys = svc.group_company_keys(bl)
    elif group_company:
        group_keys = set()
        for bl in db.query(BankLimit).options(joinedload(BankLimit.lines)).all():
            if matches_company_code(cr, bl.group_company, group_company):
                group_keys |= svc.group_company_keys(bl)

    rows = []
    for lc in svc.open_lcs_for_bank(db, bank, df, dt, group_keys):
        if company and not matches_company_code(cr, lc.importer_name, company):
            continue
        if not svc.type_applies(want_lc, svc.lc_tenor(lc)):
            continue
        row = svc.lc_row(lc, branch)
        row["company_name_raw"] = lc.importer_name
        row["company_name"] = cr.resolve(lc.importer_name).get("short_code") or row["company_name"]
        rows.append(row)
    rows.sort(key=lambda r: (0 if r.get("missing_rate") else 1, r["company_name"] or "", r["lc_date"] or ""))
    return {"count": len(rows), "bank_name": bank, "company": company,
            "missing_rate_count": sum(1 for r in rows if r.get("missing_rate")),
            "items": rows}
