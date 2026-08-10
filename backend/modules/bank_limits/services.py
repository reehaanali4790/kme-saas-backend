"""Bank Limit business logic.
"""
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload, selectinload
from core.exceptions import NotFoundError, ValidationError
from models.database_models import BankLimit, BankLimitLine, LCMaster
from infrastructure.normalization.normalization_service import (
    norm_bank, normalize_bank, normalize_importer, payment_tenor,
    company_key, company_resolver,
)
from infrastructure.audit.audit_service import log_audit
from .schemas import (
    BANK_LIMIT_TYPE_LABEL,
    BankLimitCreate,
    BankLimitLineIn,
    BankLimitLineOut,
    BankLimitOut,
    BankLimitUpdate,
    LC_TYPES,
)


def norm_lc_type(v, default: str = "BOTH") -> str:
    v = str(v or "").strip().upper()
    return v if v in LC_TYPES else default


def norm_limit_type(v) -> str:
    """Company line role. PARENT stays PARENT; everything else (incl. legacy SUB) -> CHILD."""
    return "PARENT" if str(v or "").strip().upper() == "PARENT" else "CHILD"


def is_parent(line: BankLimitLine) -> bool:
    return (line.limit_type or "").upper() == "PARENT"


def lc_tenor(lc: LCMaster) -> Optional[str]:
    """LC tenor SIGHT/DA from payment terms; None when it can't be told."""
    return payment_tenor(lc.payment_terms)


def type_applies(limit_lc_type, tenor: Optional[str]) -> bool:
    """Does an LC of `tenor` (SIGHT/DA/None) fall under a limit/line of `limit_lc_type`?
    BOTH covers everything; a specific type needs an exactly-matching known tenor."""
    lt = norm_lc_type(limit_lc_type)
    if lt == "BOTH":
        return True
    return tenor is not None and tenor.upper() == lt


def lc_currency(lc: LCMaster) -> str:
    """Currency letters only (data has '$ USD' etc.); default USD."""
    return re.sub(r"[^A-Z]", "", (lc.currency or "USD").upper()) or "USD"


def lc_amount(lc: LCMaster) -> float:
    """LC value = sum of product lc_amount, falling back to qty * unit price."""
    amt = sum(float(p.lc_amount) for p in lc.products if p.lc_amount is not None)
    if not amt:
        amt = sum(float(p.quantity or 0) * float(p.lc_unit_price or 0) for p in lc.products)
    return round(amt, 2)


def lc_pkr_rate(lc: LCMaster) -> Tuple[Optional[float], Optional[str]]:
    """USD/other -> PKR rate for this LC. Prefer a shipment's booked exchange_rate, then the
    LC's exchange_rate. Returns (rate_or_None, source)."""
    for s in lc.shipments:
        if s.exchange_rate is not None:
            return float(s.exchange_rate), "shipment"
    if lc.exchange_rate is not None:
        return float(lc.exchange_rate), "lc"
    return None, None


def lc_is_utilized(lc: LCMaster) -> bool:
    """True while the LC still consumes the bank limit (shipment-status driven).
    Released when the LC is CANCELLED/CLOSED, or every shipment is settled
    (payment_date or retirement_date set, or shipment status DELIVERED — the terminal
    shipment status; the legacy "CLOSED" shipment status can no longer occur since
    status is now fully derived, see modules/shipments/services.py)."""
    st = (lc.status or "").upper()
    if st in ("CANCELLED", "CLOSED"):
        return False
    ships = lc.shipments
    if not ships:
        return True   # issued, nothing shipped/settled yet -> committed
    for s in ships:
        sst = (s.status or "").upper()
        if not (s.payment_date or s.retirement_date or sst in ("DELIVERED", "CLOSED")):
            return True   # an open shipment -> still utilized
    return False          # all shipments settled -> released


def display_limit_type(ln: BankLimitLine) -> str:
    """Normalise the stored role for the UI: PARENT or CHILD (legacy SUB -> CHILD)."""
    return "PARENT" if is_parent(ln) else "CHILD"


def line_to_schema(ln: BankLimitLine) -> BankLimitLineOut:
    return BankLimitLineOut(
        line_id=ln.line_id,
        company_name=ln.company_name,
        limit_type=display_limit_type(ln),
        lc_type=norm_lc_type(ln.lc_type),
        sub_limit_amount=float(ln.sub_limit_amount) if ln.sub_limit_amount is not None else None,
    )


def limit_to_schema(bl: BankLimit) -> BankLimitOut:
    return BankLimitOut(
        limit_id=bl.limit_id,
        bank_name=bl.bank_name,
        branch=bl.branch,
        group_company=bl.group_company,
        bank_limit_type=(bl.bank_limit_type or "REGULAR"),
        bank_limit_type_label=BANK_LIMIT_TYPE_LABEL.get(
            bl.bank_limit_type or "REGULAR", bl.bank_limit_type or "Regular"
        ),
        lc_type=norm_lc_type(bl.lc_type),
        valid_from=bl.valid_from,
        valid_to=bl.valid_to,
        group_limit_amount=float(bl.group_limit_amount) if bl.group_limit_amount is not None else None,
        revision_no=bl.revision_no,
        remarks=bl.remarks,
        created_at=bl.created_at,
        lines=[line_to_schema(l) for l in sorted(bl.lines, key=lambda x: x.line_id)],
    )


def line_conflict_warnings(limit_lc_type, lines: List[BankLimitLineIn]) -> List[str]:
    """Warn (not block) when a company line's LC type can't be served by the limit's LC type.
    e.g. a Sight-only limit with a DA sub-limit line."""
    warns = []
    lt = norm_lc_type(limit_lc_type)
    if lt == "BOTH":
        return warns
    for line in lines or []:
        name = (line.company_name or "").strip()
        clt = norm_lc_type(line.lc_type)
        if name and clt != "BOTH" and clt != lt:
            warns.append(f"{name}: line LC type ({clt}) differs from the limit LC type ({lt}).")
    return warns


def apply_lines(bl: BankLimit, lines: Optional[List[BankLimitLineIn]], db: Session) -> None:
    bl.lines.clear()
    for line in lines or []:
        name = (line.company_name or "").strip()
        if not name:
            continue
        canon = normalize_importer(name, db)
        if canon:
            name = canon
        bl.lines.append(BankLimitLine(
            company_name=name,
            limit_type=norm_limit_type(line.limit_type),
            lc_type=norm_lc_type(line.lc_type),
            sub_limit_amount=line.sub_limit_amount,
        ))


def group_company_keys(bl: BankLimit) -> set:
    """Normalized importer keys for every company under one parent group."""
    keys = set()
    if bl.group_company:
        keys.add(company_key(bl.group_company))
    for ln in bl.lines or []:
        if ln.company_name:
            keys.add(company_key(ln.company_name))
    return keys


def lc_in_group(lc: LCMaster, group_keys: set) -> bool:
    return company_key(lc.importer_name) in group_keys


def ensure_parent_line(bl: BankLimit, db: Session) -> None:
    """Parent line must match group_company — create or align automatically."""
    grp = (bl.group_company or "").strip()
    if not grp:
        return
    canon = normalize_importer(grp, db) or grp
    parent = next((ln for ln in bl.lines if is_parent(ln)), None)
    if parent:
        if company_key(parent.company_name) != company_key(canon):
            parent.company_name = canon
    else:
        bl.lines.append(BankLimitLine(
            company_name=canon, limit_type="PARENT",
            lc_type=norm_lc_type(bl.lc_type), sub_limit_amount=None))


def list_limits(db: Session, bank: Optional[str], group_company: Optional[str]) -> List[BankLimit]:
    q = db.query(BankLimit).options(joinedload(BankLimit.lines))
    if bank:
        q = q.filter(BankLimit.bank_name == bank)
    if group_company:
        q = q.filter(BankLimit.group_company == group_company)
    return q.order_by(BankLimit.bank_name, BankLimit.group_company,
                       BankLimit.valid_from.desc()).all()


def get_limit_options(db: Session) -> dict:
    """Distinct banks / branches / groups / companies already used in bank_limits, for the
    report filter dropdowns. Groups/companies are returned as short codes so the dropdown
    matches what the report's company filter (matches_company_code) expects."""
    rows = db.query(BankLimit).options(joinedload(BankLimit.lines)).all()
    cr = company_resolver(db)
    banks, branches, groups, companies = set(), set(), set(), set()
    for r in rows:
        if r.bank_name:
            banks.add(r.bank_name)
        if r.branch:
            branches.add(r.branch)
        if r.group_company:
            code = cr.resolve(r.group_company).get("short_code")
            if code:
                groups.add(code)
        for ln in r.lines:
            if ln.company_name:
                code = cr.resolve(ln.company_name).get("short_code")
                if code:
                    companies.add(code)
    return {"banks": sorted(banks), "branches": sorted(branches),
            "groups": sorted(groups), "companies": sorted(companies)}


def get_limit_or_404(db: Session, limit_id: int) -> BankLimit:
    bl = db.query(BankLimit).options(joinedload(BankLimit.lines)).filter(
        BankLimit.limit_id == limit_id).first()
    if not bl:
        raise NotFoundError("Bank limit not found")
    return bl


def create_limit(db: Session, data: BankLimitCreate, created_by: int) -> Tuple[BankLimit, List[str]]:
    bank = normalize_bank(data.bank_name, db) if data.bank_name else None
    if not bank or bank == "(Unknown Bank)":
        raise ValidationError("Bank Name is required.")
    group_company = normalize_importer(data.group_company, db) or data.group_company

    # revision number = next within this bank + group + limit-type (a temporary/one-off is
    # its own series, not a revision of the regular limit).
    prev = db.query(BankLimit).filter(
        BankLimit.bank_name == bank,
        BankLimit.group_company == group_company,
        BankLimit.bank_limit_type == data.bank_limit_type).count()

    bl = BankLimit(
        bank_name=bank, branch=data.branch,
        group_company=group_company, bank_limit_type=data.bank_limit_type,
        lc_type=data.lc_type, valid_from=data.valid_from, valid_to=data.valid_to,
        group_limit_amount=data.group_limit_amount,
        revision_no=prev + 1, remarks=data.remarks,
        created_by=created_by)
    apply_lines(bl, data.lines, db)
    ensure_parent_line(bl, db)
    db.add(bl)
    db.flush()
    limit_amount = bl.group_limit_amount
    log_audit(db, created_by, "CREATE_BANK_LIMIT", entity_type="BANK_LIMIT", entity_id=bl.limit_id,
              new_value={"bank_name": bl.bank_name, "group_company": bl.group_company,
                        "group_limit_amount": str(limit_amount) if limit_amount is not None else None},
              description=f"Created bank limit for '{bank}' / {data.group_company}")
    db.commit()
    db.refresh(bl)
    return bl, line_conflict_warnings(data.lc_type, data.lines)


def update_limit(db: Session, limit_id: int, data: BankLimitUpdate, updated_by: int) -> Tuple[BankLimit, List[str]]:
    bl = db.query(BankLimit).filter(BankLimit.limit_id == limit_id).first()
    if not bl:
        raise NotFoundError("Bank limit not found")

    if data.bank_name:
        bl.bank_name = normalize_bank(data.bank_name, db) or norm_bank(data.bank_name)
    if data.branch is not None:
        bl.branch = data.branch
    if data.group_company:
        bl.group_company = normalize_importer(data.group_company, db) or data.group_company
    if data.bank_limit_type:
        bl.bank_limit_type = data.bank_limit_type
    if data.lc_type:
        bl.lc_type = data.lc_type
    if data.valid_from:
        bl.valid_from = data.valid_from
    if data.valid_to:
        bl.valid_to = data.valid_to
    if bl.valid_from and bl.valid_to and bl.valid_to < bl.valid_from:
        raise ValidationError("Valid To cannot be before Valid From.")
    if data.group_limit_amount is not None:
        bl.group_limit_amount = data.group_limit_amount
    if data.remarks is not None:
        bl.remarks = data.remarks
    if data.lines is not None:
        apply_lines(bl, data.lines, db)
    ensure_parent_line(bl, db)
    bl.updated_at = datetime.utcnow()
    bl.updated_by = updated_by
    log_audit(db, updated_by, "UPDATE_BANK_LIMIT", entity_type="BANK_LIMIT", entity_id=bl.limit_id,
              description=f"Updated bank limit for '{bl.bank_name}' / {bl.group_company}")
    db.commit()
    return bl, line_conflict_warnings(bl.lc_type, data.lines)


def delete_limit(db: Session, limit_id: int, deleted_by: int) -> None:
    bl = db.query(BankLimit).filter(BankLimit.limit_id == limit_id).first()
    if not bl:
        raise NotFoundError("Bank limit not found")
    log_audit(db, deleted_by, "DELETE_BANK_LIMIT", entity_type="BANK_LIMIT", entity_id=bl.limit_id,
              old_value={"bank_name": bl.bank_name, "group_company": bl.group_company},
              description=f"Deleted bank limit for '{bl.bank_name}' / {bl.group_company}")
    db.delete(bl)
    db.commit()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def open_lcs_for_bank(db: Session, bank_canon: str, df: date, dt: date,
                      group_keys: Optional[set] = None) -> List[LCMaster]:
    """All utilized (open) LCs for a single canonical bank within [df,dt], eager-loaded.
    Used by the drill-down endpoint (one bank per call). The main bank-limit report
    covers many (bank, branch, group, type) contexts per request — use
    open_lcs_by_bank_all() there instead of calling this once per context.

    When group_keys is set, only LCs whose importer belongs to that parent group are
    included — limits of different parent companies never mix."""
    q = db.query(LCMaster).options(
        selectinload(LCMaster.products), selectinload(LCMaster.shipments)
    ).filter(LCMaster.lc_date >= df, LCMaster.lc_date <= dt)
    out = []
    for lc in q.all():
        if norm_bank(lc.bank_name) != bank_canon:
            continue
        if group_keys is not None and not lc_in_group(lc, group_keys):
            continue
        if not lc_is_utilized(lc):
            continue
        out.append(lc)
    return out


def open_lcs_by_bank_all(db: Session, df: date, dt: date) -> Dict[str, List[LCMaster]]:
    """All utilized (open) LCs within [df,dt], grouped by canonical bank name, in ONE query.

    The bank-limit report groups revisions into (bank, branch, group, type) contexts and
    used to call open_lcs_for_bank() once per context — a bank with 4 branches x 3 limit
    types re-ran the same full-range "load every LC, filter to this bank in Python" query
    12 times. This loads the date-range window once and buckets it by bank, so a whole
    report request costs one query regardless of how many contexts share the same bank."""
    q = db.query(LCMaster).options(
        selectinload(LCMaster.products), selectinload(LCMaster.shipments)
    ).filter(LCMaster.lc_date >= df, LCMaster.lc_date <= dt)
    grouped: Dict[str, List[LCMaster]] = defaultdict(list)
    for lc in q.all():
        if not lc_is_utilized(lc):
            continue
        grouped[norm_bank(lc.bank_name)].append(lc)
    return grouped


def lc_row(lc: LCMaster, branch: Optional[str]) -> dict:
    """Drill-down row for one utilized LC (amount + PKR conversion + status)."""
    amt = lc_amount(lc)
    cur = lc_currency(lc)
    rate, src = lc_pkr_rate(lc)
    if cur == "PKR":
        pkr, missing = amt, False
    elif rate:
        pkr, missing = round(amt * rate, 2), False
    else:
        pkr, missing = None, True
    ship = lc.shipments[0] if lc.shipments else None
    return {
        "lc_id": lc.lc_id, "lc_number": lc.lc_number, "company_name": lc.importer_name,
        "bank_name": norm_bank(lc.bank_name), "branch": branch,
        "lc_type": lc_tenor(lc),
        "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
        "lc_amount": amt, "currency": cur,
        "exchange_rate": rate, "rate_source": src,
        "lc_amount_pkr": pkr, "missing_rate": missing,
        "shipment_status": (ship.status if ship else (lc.status or None)),
        "payment_date": ship.payment_date.isoformat() if ship and ship.payment_date else None,
        "delivery_date": ship.delivery_date.isoformat() if ship and ship.delivery_date else None,
        "retirement_date": ship.retirement_date.isoformat() if ship and ship.retirement_date else None,
    }


def lc_brief(row: dict) -> dict:
    """Compact per-LC contribution shown inline under a company row in the report."""
    return {k: row[k] for k in ("lc_id", "lc_number", "lc_date", "lc_type", "lc_amount",
                                 "currency", "exchange_rate", "lc_amount_pkr", "missing_rate",
                                 "shipment_status")}


def validity_status(bl: BankLimit) -> str:
    return "Valid" if bl.valid_to and bl.valid_to >= date.today() else "Expired"
