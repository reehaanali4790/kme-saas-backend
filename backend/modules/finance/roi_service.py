"""
ROI Intelligence — cash exposure, payables forecasting, bank utilization, LME opportunity.

Aggregates existing operational data (LCs, shipments, GD balance, bank limits, demurrage,
alerts) into monetary KPIs and forward-looking buckets. Additive layer on top of existing
reports; does not replace them.

Ported from rehan/newchanges (backend/services/roi_intelligence_service.py).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    BankLimit, BillOfLading, DemurrageConfig, GoodsDeclaration,
    LCMaster, LCProduct, Shipment, SystemAlert,
)
from modules.shipments.demurrage_service import compute_demurrage
from modules.weboc.helpers.gd_balance_report import gd_balance_report
from infrastructure.normalization.normalization_service import (
    company_resolver, matches_company_code, norm_bank, payment_tenor,
)
from modules.reports.service import pending_order_report
from modules.shipments.shipment_metrics import resolve_amount

logger = logging.getLogger("uvicorn")

_BUCKETS = ("0_30", "31_60", "61_90", "91_plus", "overdue", "no_date")
T = TypeVar("T")


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _cur(lc: LCMaster) -> str:
    return re.sub(r"[^A-Z]", "", (lc.currency or "USD").upper()) or "USD"


def _lc_amount(lc: LCMaster) -> float:
    products = lc.products or []
    amt = sum(float(p.lc_amount) for p in products if p.lc_amount is not None)
    if not amt:
        amt = sum(float(p.quantity or 0) * float(p.lc_unit_price or 0) for p in products)
    return round(amt, 2)


def _rev_no(v) -> int:
    """Safe revision number for sorting — NULL revision_no must not break comparisons."""
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _lc_window_start(today: date) -> date:
    """LC lookback window — avoid date.replace() leap-year edge cases."""
    return today - timedelta(days=730)


def _lc_pkr_rate(lc: LCMaster) -> tuple[Optional[float], bool]:
    for s in lc.shipments or []:
        if getattr(s, "is_deleted", False):
            continue
        if s.exchange_rate is not None:
            return float(s.exchange_rate), False
    if lc.exchange_rate is not None:
        return float(lc.exchange_rate), False
    return None, True


def _to_pkr(lc: LCMaster, amount: float) -> tuple[Optional[float], bool]:
    if _cur(lc) == "PKR":
        return round(amount, 2), False
    rate, missing = _lc_pkr_rate(lc)
    if rate:
        return round(amount * rate, 2), False
    return None, True


def _bucket(days: Optional[int]) -> str:
    if days is None:
        return "no_date"
    if days < 0:
        return "overdue"
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "91_plus"


def _empty_buckets() -> Dict[str, float]:
    return {b: 0.0 for b in _BUCKETS}


@dataclass
class RoiFilters:
    """Each field accepts a list of values — multiple values within a field are OR'd
    together; the fields themselves AND. None / empty list = no filter on that field."""
    importer: Optional[List[str]] = None
    buyer: Optional[List[str]] = None
    vessel: Optional[List[str]] = None
    bank: Optional[List[str]] = None

    def active(self) -> bool:
        return bool(self.importer or self.buyer or self.vessel or self.bank)

    def as_dict(self) -> Dict[str, Optional[List[str]]]:
        return {
            k: v for k, v in {
                "importer": self.importer,
                "buyer": self.buyer,
                "vessel": self.vessel,
                "bank": self.bank,
            }.items() if v
        }


def _buyer_label(lc: Optional[LCMaster]) -> Optional[str]:
    if not lc:
        return None
    if lc.booked_by and lc.booked_by.strip():
        return lc.booked_by.strip()
    names = [a.buyer_name.strip() for a in (lc.buyer_allocations or []) if a.buyer_name]
    if not names:
        return None
    return "+".join(names) if len(names) > 1 else names[0]


def _company_meta(lc: Optional[LCMaster], resolver) -> tuple[Optional[str], Optional[str], bool]:
    if not lc or not lc.importer_name:
        return None, None, False
    res = resolver.resolve(lc.importer_name)
    code = res.get("short_code") or lc.importer_name
    return lc.importer_name, code, bool(res.get("matched"))


def _vessel_for_lc(lc: Optional[LCMaster], ship: Optional[Shipment] = None) -> Optional[str]:
    if ship and ship.vessel_name:
        return ship.vessel_name.strip()
    if not lc:
        return None
    for s in lc.shipments or []:
        if getattr(s, "is_deleted", False):
            continue
        if s.vessel_name:
            return s.vessel_name.strip()
    return (lc.vessel_name or "").strip() or None


def _row_matches_filters(row: dict, filters: Optional[RoiFilters], resolver) -> bool:
    if not filters or not filters.active():
        return True
    if filters.importer:
        imp = row.get("importer")
        code = row.get("company_code")
        matched_any = False
        for one in filters.importer:
            if matches_company_code(resolver, imp, one):
                matched_any = True
                break
            needle = one.strip().lower()
            if code and needle in code.lower():
                matched_any = True
                break
            if imp and needle in imp.lower():
                matched_any = True
                break
        if not matched_any:
            return False
    if filters.buyer:
        buyer_val = (row.get("buyer") or "").lower()
        if not any(b.strip().lower() in buyer_val for b in filters.buyer):
            return False
    if filters.vessel:
        v = (row.get("vessel") or row.get("vessel_name") or "").lower()
        if not any(x.strip().lower() in v for x in filters.vessel):
            return False
    if filters.bank:
        rb = norm_bank(row.get("bank_name") or row.get("bank") or "")
        if not any(rb == norm_bank(b) for b in filters.bank):
            return False
    return True


def _agg_dimension(
    rows: List[dict],
    key_field: str,
    label_field: str,
    sum_fields: Dict[str, str],
    count_field: str = "count",
) -> List[dict]:
    groups: Dict[str, dict] = {}
    for r in rows:
        key = (r.get(key_field) or "").strip() or "(Unknown)"
        label = (r.get(label_field) or key).strip() if label_field else key
        if key not in groups:
            groups[key] = {"key": key, "label": label, count_field: 0}
            for sk in sum_fields:
                groups[key][sk] = 0.0
        groups[key][count_field] += 1
        for sk, rf in sum_fields.items():
            val = r.get(rf)
            if val is not None:
                try:
                    groups[key][sk] += float(val)
                except (TypeError, ValueError):
                    pass
    out = list(groups.values())
    for g in out:
        for sk in sum_fields:
            g[sk] = round(g[sk], 2)
    primary = next(iter(sum_fields), None)
    out.sort(key=lambda x: x.get(primary, 0) if primary else x[count_field], reverse=True)
    return out


def roi_filter_options(db: Session) -> Dict[str, Any]:
    """Distinct importers, buyers, vessels, banks for ROI filter dropdowns."""
    resolver = company_resolver(db)
    importers: Dict[str, str] = {}
    buyers, vessels, banks = set(), set(), set()

    lcs = (
        db.query(LCMaster)
        .options(joinedload(LCMaster.buyer_allocations), joinedload(LCMaster.shipments))
        .filter(LCMaster.status.in_(("OPEN", "SHIPPED")))
        .all()
    )
    for lc in lcs:
        _raw, code, _ = _company_meta(lc, resolver)
        if code:
            importers[code] = lc.importer_name or code
        buyer = _buyer_label(lc)
        if buyer:
            buyers.add(buyer)
        vessel = _vessel_for_lc(lc)
        if vessel:
            vessels.add(vessel)
        if lc.bank_name:
            banks.add(norm_bank(lc.bank_name))

    for rep in (gd_balance_report(db, gd_type="INTO_BOND", include_settled=False),
                gd_balance_report(db, gd_type="HOME_CONSUMPTION", include_settled=False)):
        for r in rep.get("rows", []):
            code = r.get("company_code")
            if code:
                importers[code] = r.get("importer") or code
            if r.get("buyer"):
                buyers.add(r["buyer"])
            if r.get("vessel_name"):
                vessels.add(r["vessel_name"])
            if r.get("bank_name"):
                banks.add(r["bank_name"])

    return {
        "importers": [{"code": k, "name": v} for k, v in sorted(importers.items())],
        "buyers": sorted(buyers),
        "vessels": sorted(vessels),
        "banks": sorted(banks),
    }


def dimension_rollup(
    lc_pay: dict,
    bank: dict,
    gd: dict,
    lme: dict,
) -> Dict[str, List[dict]]:
    """Unified cross-module rollup per importer / buyer / vessel."""
    def _merge(target: Dict[str, dict], key: str, label: str, **metrics):
        if not key:
            key = "(Unknown)"
        if key not in target:
            target[key] = {
                "key": key, "label": label or key,
                "lc_payables_pkr": 0.0, "bank_utilized_pkr": 0.0, "gd_duty_pkr": 0.0,
                "gd_penalty_pkr": 0.0, "lme_favorable_usd": 0.0, "lme_adverse_usd": 0.0,
                "open_lc_count": 0, "open_gd_count": 0, "lme_line_count": 0,
            }
        for mk, mv in metrics.items():
            if mk in target[key] and mv is not None:
                target[key][mk] += float(mv)

    by_imp: Dict[str, dict] = {}
    by_buy: Dict[str, dict] = {}
    by_ves: Dict[str, dict] = {}

    for r in lc_pay.get("rows", []):
        code = r.get("company_code") or "(Unknown)"
        pkr = r.get("amount_pkr") or 0
        if not r.get("missing_rate"):
            _merge(by_imp, code, r.get("importer"), lc_payables_pkr=pkr)
            _merge(by_buy, r.get("buyer") or "(Unknown)", r.get("buyer"), lc_payables_pkr=pkr)
            _merge(by_ves, r.get("vessel") or "(Unknown)", r.get("vessel"), lc_payables_pkr=pkr)

    for r in bank.get("lc_utilization_rows", []):
        code = r.get("company_code") or "(Unknown)"
        pkr = r.get("utilized_pkr") or 0
        _merge(by_imp, code, r.get("importer"), bank_utilized_pkr=pkr, open_lc_count=1)
        _merge(by_buy, r.get("buyer") or "(Unknown)", r.get("buyer"), bank_utilized_pkr=pkr, open_lc_count=1)
        _merge(by_ves, r.get("vessel") or "(Unknown)", r.get("vessel"), bank_utilized_pkr=pkr, open_lc_count=1)

    for r in gd.get("rows", []):
        code = r.get("company_code") or "(Unknown)"
        duty = r.get("remaining_duty_pkr") or 0
        pen = r.get("penalty_pkr") or 0
        _merge(by_imp, code, r.get("importer"), gd_duty_pkr=duty, gd_penalty_pkr=pen, open_gd_count=1)
        _merge(by_buy, r.get("buyer") or "(Unknown)", r.get("buyer"), gd_duty_pkr=duty, gd_penalty_pkr=pen, open_gd_count=1)
        _merge(by_ves, r.get("vessel") or "(Unknown)", r.get("vessel"), gd_duty_pkr=duty, gd_penalty_pkr=pen, open_gd_count=1)

    for r in lme.get("rows", []):
        code = r.get("company_code") or "(Unknown)"
        fav = abs(r["impact_usd"]) if r.get("direction") == "favorable" and r.get("impact_usd") else 0
        adv = (r.get("impact_usd") or 0) if r.get("direction") == "adverse" else 0
        _merge(by_imp, code, r.get("importer"), lme_favorable_usd=fav, lme_adverse_usd=adv, lme_line_count=1)
        _merge(by_buy, r.get("buyer") or "(Unknown)", r.get("buyer"), lme_favorable_usd=fav, lme_adverse_usd=adv, lme_line_count=1)
        _merge(by_ves, r.get("vessel") or "(Unknown)", r.get("vessel"), lme_favorable_usd=fav, lme_adverse_usd=adv, lme_line_count=1)

    def _finalize(groups: Dict[str, dict]) -> List[dict]:
        rows = list(groups.values())
        for g in rows:
            for k in ("lc_payables_pkr", "bank_utilized_pkr", "gd_duty_pkr", "gd_penalty_pkr",
                      "lme_favorable_usd", "lme_adverse_usd"):
                g[k] = round(g[k], 2)
        rows.sort(key=lambda x: x["gd_duty_pkr"] + x["lc_payables_pkr"] + x["bank_utilized_pkr"], reverse=True)
        return rows

    return {
        "by_importer": _finalize(by_imp),
        "by_buyer": _finalize(by_buy),
        "by_vessel": _finalize(by_ves),
    }


def _payable_due_date(shipment: Shipment, lc: LCMaster) -> Optional[date]:
    if shipment.maturity_date:
        return shipment.maturity_date
    if lc.doc_payment_date:
        return lc.doc_payment_date
    if shipment.eta:
        return shipment.eta
    if lc.eta:
        return lc.eta
    return None


def lc_payables_forecast(
    db: Session,
    *,
    horizon_days: int = 90,
    filters: Optional[RoiFilters] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Unpaid shipment payables grouped by bank and time bucket (PKR where rate known)."""
    today = date.today()
    resolver = company_resolver(db)
    shipments = (
        db.query(Shipment)
        .options(
            joinedload(Shipment.lc).joinedload(LCMaster.products),
            joinedload(Shipment.lc).joinedload(LCMaster.buyer_allocations),
            joinedload(Shipment.lc).joinedload(LCMaster.shipments),
            joinedload(Shipment.commercial_invoices),
        )
        .filter(Shipment.is_deleted.is_(False), Shipment.payment_date.is_(None))
        .all()
    )

    by_bank: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []

    for ship in shipments:
        lc = ship.lc
        if not lc or (lc.status or "").upper() in ("CANCELLED", "CLOSED"):
            continue
        if ship.retirement_date:
            continue

        importer, company_code, _ = _company_meta(lc, resolver)
        buyer = _buyer_label(lc)
        vessel = _vessel_for_lc(lc, ship)

        amt_usd, cur, _src = resolve_amount(ship)
        if amt_usd is None:
            amt_usd = _lc_amount(lc)
            cur = _cur(lc)
        if cur == "PKR":
            pkr, missing = round(amt_usd, 2), False
        else:
            pkr, missing = _to_pkr(lc, amt_usd)

        due = _payable_due_date(ship, lc)
        days = (due - today).days if due else None
        bucket = _bucket(days)
        bank = norm_bank(lc.bank_name) or "(Unknown)"

        row = {
            "shipment_id": ship.shipment_id,
            "lc_id": lc.lc_id,
            "lc_number": lc.lc_number,
            "bank_name": bank,
            "importer": importer,
            "company_code": company_code,
            "buyer": buyer,
            "vessel": vessel,
            "due_date": due.isoformat() if due else None,
            "days_until_due": days,
            "bucket": bucket,
            "amount": amt_usd,
            "currency": cur,
            "amount_pkr": pkr if not missing else None,
            "missing_rate": missing,
            "tenor": payment_tenor(lc.payment_terms),
        }
        if not _row_matches_filters(row, filters, resolver):
            continue
        rows.append(row)

    totals = {"count": 0, "amount_pkr": 0.0, "missing_rate_count": 0, "buckets": _empty_buckets()}
    for row in rows:
        bank = row["bank_name"]
        bucket = row["bucket"]
        missing = row["missing_rate"]
        pkr = row["amount_pkr"]

        if bank not in by_bank:
            by_bank[bank] = {
                "bank_name": bank,
                "count": 0,
                "amount_pkr": 0.0,
                "missing_rate_count": 0,
                "buckets": _empty_buckets(),
            }
        grp = by_bank[bank]
        grp["count"] += 1
        totals["count"] += 1
        if missing:
            grp["missing_rate_count"] += 1
            totals["missing_rate_count"] += 1
        elif pkr:
            grp["amount_pkr"] += pkr
            grp["buckets"][bucket] += pkr
            totals["amount_pkr"] += pkr
            totals["buckets"][bucket] += pkr

    rows.sort(key=lambda r: (
        r["days_until_due"] if r["days_until_due"] is not None else 99999,
        r["lc_number"] or "",
    ))
    banks = sorted(by_bank.values(), key=lambda b: b["amount_pkr"], reverse=True)
    for b in banks:
        b["amount_pkr"] = round(b["amount_pkr"], 2)
        for k in b["buckets"]:
            b["buckets"][k] = round(b["buckets"][k], 2)
    totals["amount_pkr"] = round(totals["amount_pkr"], 2)
    for k in totals["buckets"]:
        totals["buckets"][k] = round(totals["buckets"][k], 2)

    total_rows = len(rows)
    page_rows = rows
    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        page_rows = rows[offset:offset + page_size]

    return {
        "as_of": today.isoformat(),
        "horizon_days": horizon_days,
        "filters": filters.as_dict() if filters else {},
        "totals": totals,
        "by_bank": banks,
        "by_importer": _agg_dimension(rows, "company_code", "importer", {"amount_pkr": "amount_pkr"}),
        "by_buyer": _agg_dimension(rows, "buyer", "buyer", {"amount_pkr": "amount_pkr"}),
        "by_vessel": _agg_dimension(rows, "vessel", "vessel", {"amount_pkr": "amount_pkr"}),
        "rows": page_rows,
        "total_rows": total_rows,
        "page": page,
        "page_size": page_size,
    }


def bank_exposure_summary(db: Session, *, filters: Optional[RoiFilters] = None) -> Dict[str, Any]:
    """Active bank limits vs live LC utilization + pending-order pipeline per bank."""
    today = date.today()
    lc_window_start = _lc_window_start(today)
    resolver = company_resolver(db)

    active_limits: Dict[tuple, BankLimit] = {}
    try:
        limits = (
            db.query(BankLimit)
            .options(joinedload(BankLimit.lines))
            .filter(BankLimit.valid_from <= today, BankLimit.valid_to >= today)
            .all()
        )
        for bl in limits:
            key = (bl.bank_name, bl.branch or "", bl.group_company, bl.bank_limit_type or "REGULAR")
            prev = active_limits.get(key)
            bl_key = (bl.valid_from, _rev_no(bl.revision_no))
            prev_key = (prev.valid_from, _rev_no(prev.revision_no)) if prev else None
            if not prev or bl_key > prev_key:
                active_limits[key] = bl
    except SQLAlchemyError as exc:
        logger.warning("bank_exposure_summary: bank_limits unavailable: %s", exc)

    offered_by_bank: Dict[str, float] = defaultdict(float)
    for bl in active_limits.values():
        offered_by_bank[norm_bank(bl.bank_name) or "(Unknown)"] += float(bl.group_limit_amount or 0)

    utilized_by_bank: Dict[str, float] = defaultdict(float)
    missing_by_bank: Dict[str, int] = defaultdict(int)
    open_lc_count: Dict[str, int] = defaultdict(int)
    lc_by_id: Dict[int, LCMaster] = {}

    open_lcs = (
        db.query(LCMaster)
        .options(
            joinedload(LCMaster.products),
            joinedload(LCMaster.shipments),
            joinedload(LCMaster.buyer_allocations),
        )
        .filter(LCMaster.lc_date >= lc_window_start)
        .all()
    )
    lc_util_rows: List[dict] = []
    for lc in open_lcs:
        lc_by_id[lc.lc_id] = lc
        st = (lc.status or "").upper()
        if st in ("CANCELLED", "CLOSED"):
            continue
        ships = [s for s in (lc.shipments or []) if not getattr(s, "is_deleted", False)]
        utilized = True
        if ships:
            utilized = any(
                not (s.payment_date or s.retirement_date or (s.status or "").upper() == "CLOSED")
                for s in ships
            )
        if not utilized:
            continue
        bank = norm_bank(lc.bank_name) or "(Unknown)"
        importer, company_code, _ = _company_meta(lc, resolver)
        buyer = _buyer_label(lc)
        vessel = _vessel_for_lc(lc)
        pkr, missing = _to_pkr(lc, _lc_amount(lc))
        util_row = {
            "lc_id": lc.lc_id,
            "lc_number": lc.lc_number,
            "bank_name": bank,
            "importer": importer,
            "company_code": company_code,
            "buyer": buyer,
            "vessel": vessel,
            "utilized_pkr": pkr if not missing else None,
            "pipeline_pkr": 0.0,
            "missing_rate": missing,
            "source": "open_lc",
        }
        if not _row_matches_filters(util_row, filters, resolver):
            continue
        lc_util_rows.append(util_row)
        open_lc_count[bank] += 1
        if missing:
            missing_by_bank[bank] += 1
        elif pkr:
            utilized_by_bank[bank] += pkr

    pending = pending_order_report(db)
    pipeline_by_bank: Dict[str, float] = defaultdict(float)
    for row in pending.get("rows", []):
        bank = norm_bank(row.get("bank")) or "(Unknown)"
        amt = row.get("lc_amount")
        if amt is None:
            continue
        lc_id = row.get("lc_id")
        lc = lc_by_id.get(lc_id) if lc_id else None
        if lc is None and lc_id:
            lc = db.get(LCMaster, lc_id)
        importer, company_code, _ = _company_meta(lc, resolver) if lc else (None, None, False)
        buyer = row.get("buyer") or (_buyer_label(lc) if lc else None)
        vessel = row.get("vessel") or (_vessel_for_lc(lc) if lc else None)
        pkr = None
        if lc:
            pkr, missing = _to_pkr(lc, float(amt))
        else:
            missing = True
        pipe_row = {
            "lc_id": lc_id,
            "lc_number": row.get("lc_number"),
            "bank_name": bank,
            "importer": importer,
            "company_code": company_code,
            "buyer": buyer,
            "vessel": vessel,
            "utilized_pkr": 0.0,
            "pipeline_pkr": pkr if not missing else None,
            "missing_rate": missing,
            "source": "pipeline",
        }
        if not _row_matches_filters(pipe_row, filters, resolver):
            continue
        lc_util_rows.append(pipe_row)
        if pkr:
            pipeline_by_bank[bank] += pkr

    banks = set(offered_by_bank) | set(utilized_by_bank) | set(pipeline_by_bank)
    if filters and filters.bank:
        bank_norms = {norm_bank(b) for b in filters.bank}
        banks = {b for b in banks if b in bank_norms}
    rows = []
    for bank in sorted(banks):
        offered = round(offered_by_bank.get(bank, 0), 2)
        utilized = round(utilized_by_bank.get(bank, 0), 2)
        pipeline = round(pipeline_by_bank.get(bank, 0), 2)
        projected = round(utilized + pipeline, 2)
        available = round(offered - utilized, 2) if offered else None
        projected_available = round(offered - projected, 2) if offered else None
        rows.append({
            "bank_name": bank,
            "offered_pkr": offered or None,
            "utilized_pkr": utilized,
            "available_pkr": available,
            "utilization_pct": round(utilized / offered * 100, 1) if offered else None,
            "pending_pipeline_pkr": pipeline,
            "projected_utilized_pkr": projected,
            "projected_available_pkr": projected_available,
            "projected_utilization_pct": round(projected / offered * 100, 1) if offered else None,
            "open_lc_count": open_lc_count.get(bank, 0),
            "missing_rate_count": missing_by_bank.get(bank, 0),
            "over_limit": projected_available is not None and projected_available < 0,
        })
    rows.sort(key=lambda r: r["utilized_pkr"], reverse=True)

    return {
        "as_of": today.isoformat(),
        "filters": filters.as_dict() if filters else {},
        "totals": {
            "offered_pkr": round(sum(offered_by_bank.values()), 2),
            "utilized_pkr": round(sum(utilized_by_bank.values()), 2),
            "pending_pipeline_pkr": round(sum(pipeline_by_bank.values()), 2),
            "banks_over_projected_limit": sum(1 for r in rows if r["over_limit"]),
        },
        "banks": rows,
        "lc_utilization_rows": lc_util_rows,
        "by_importer": _agg_dimension(
            lc_util_rows, "company_code", "importer",
            {"utilized_pkr": "utilized_pkr", "pipeline_pkr": "pipeline_pkr"},
        ),
        "by_buyer": _agg_dimension(
            lc_util_rows, "buyer", "buyer",
            {"utilized_pkr": "utilized_pkr", "pipeline_pkr": "pipeline_pkr"},
        ),
        "by_vessel": _agg_dimension(
            lc_util_rows, "vessel", "vessel",
            {"utilized_pkr": "utilized_pkr", "pipeline_pkr": "pipeline_pkr"},
        ),
    }


def gd_payables_forecast(
    db: Session,
    *,
    filters: Optional[RoiFilters] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Duty still owed on open GDs — Into-Bond balance + HC unsettled, by expiry week."""
    today = date.today()
    resolver = company_resolver(db)
    ib = gd_balance_report(db, gd_type="INTO_BOND", include_settled=False)
    hc = gd_balance_report(db, gd_type="HOME_CONSUMPTION", include_settled=False)

    rows: List[Dict[str, Any]] = []
    for src, rep in (("INTO_BOND", ib), ("HOME_CONSUMPTION", hc)):
        for r in rep.get("rows", []):
            potential = r.get("potential_duty_pkr") or 0
            penalty = r.get("penalty_pkr") or 0
            paid = r.get("duties_paid_pkr") or 0
            remaining = potential if potential else 0
            expiry = r.get("ib_expiry_date") or r.get("bond_expiry_date") or r.get("ib_gd_date")
            days = r.get("remaining_days")
            week_key = None
            if expiry:
                try:
                    exp_d = date.fromisoformat(str(expiry)[:10])
                    week_start = exp_d - timedelta(days=exp_d.weekday())
                    week_key = week_start.isoformat()
                except ValueError:
                    pass
            row = {
                "gd_type": src,
                "gd_number": r.get("ib_gd_number") or r.get("gd_number"),
                "lc_reference": r.get("lc_reference"),
                "lc_id": r.get("lc_id"),
                "importer": r.get("importer"),
                "company_code": r.get("company_code"),
                "buyer": r.get("buyer"),
                "vessel": r.get("vessel_name"),
                "bank_name": r.get("bank_name"),
                "potential_duty_pkr": _f(r.get("potential_duty_pkr")),
                "duties_paid_pkr": _f(paid),
                "penalty_pkr": _f(penalty),
                "remaining_duty_pkr": remaining,
                "remaining_days": days,
                "bond_expiry_date": expiry,
                "forecast_week": week_key,
                "gd_status": r.get("gd_status"),
            }
            if not _row_matches_filters(row, filters, resolver):
                continue
            rows.append(row)

    by_week: Dict[str, float] = defaultdict(float)
    for r in rows:
        if r["forecast_week"] and r["remaining_duty_pkr"]:
            by_week[r["forecast_week"]] += r["remaining_duty_pkr"]

    totals = {
        "open_gd_count": len(rows),
        "potential_duty_pkr": round(sum(r["remaining_duty_pkr"] or 0 for r in rows), 2),
        "penalty_exposed_pkr": round(sum(r["penalty_pkr"] or 0 for r in rows), 2),
        "duties_paid_pkr": round(sum(r["duties_paid_pkr"] or 0 for r in rows if r["duties_paid_pkr"]), 2),
        "due_within_30d_pkr": round(
            sum(r["remaining_duty_pkr"] or 0 for r in rows
                if r["remaining_days"] is not None and 0 <= r["remaining_days"] <= 30), 2),
        "overdue_pkr": round(
            sum(r["remaining_duty_pkr"] or 0 for r in rows
                if r["remaining_days"] is not None and r["remaining_days"] < 0), 2),
    }

    forecast_weeks = [
        {"week_start": wk, "duty_pkr": round(amt, 2)}
        for wk, amt in sorted(by_week.items())
    ]

    rows.sort(key=lambda r: (
        r["remaining_days"] if r["remaining_days"] is not None else 99999,
        r["lc_reference"] or "",
    ))

    total_rows = len(rows)
    page_rows = rows
    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        page_rows = rows[offset:offset + page_size]

    return {
        "as_of": today.isoformat(),
        "filters": filters.as_dict() if filters else {},
        "totals": totals,
        "forecast_by_week": forecast_weeks,
        "by_importer": _agg_dimension(rows, "company_code", "importer", {"remaining_duty_pkr": "remaining_duty_pkr", "penalty_pkr": "penalty_pkr"}),
        "by_buyer": _agg_dimension(rows, "buyer", "buyer", {"remaining_duty_pkr": "remaining_duty_pkr", "penalty_pkr": "penalty_pkr"}),
        "by_vessel": _agg_dimension(rows, "vessel", "vessel", {"remaining_duty_pkr": "remaining_duty_pkr", "penalty_pkr": "penalty_pkr"}),
        "rows": page_rows,
        "total_rows": total_rows,
        "page": page,
        "page_size": page_size,
    }


def lme_opportunities(
    db: Session,
    *,
    favorable_only: bool = False,
    filters: Optional[RoiFilters] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Open LC lines where current LME vs contracted unit price shows savings or risk."""
    today = date.today()
    resolver = company_resolver(db)
    products = (
        db.query(LCProduct)
        .join(LCMaster)
        .options(
            joinedload(LCProduct.lc).joinedload(LCMaster.buyer_allocations),
            joinedload(LCProduct.lc).joinedload(LCMaster.shipments),
        )
        .filter(LCMaster.status.in_(("OPEN", "SHIPPED")))
        .all()
    )

    rows: List[Dict[str, Any]] = []
    adverse_count = 0

    for p in products:
        lc = p.lc
        if not lc:
            continue
        unit = float(p.lc_unit_price or 0)
        if unit <= 0:
            continue
        current = p.current_lme or p.current_lme_price
        if current is None:
            continue
        current_f = float(current)
        diff = round(current_f - unit, 2)
        pct = round((diff / unit) * 100, 2) if unit else None
        qty = float(p.quantity or 0)
        impact_usd = round(diff * qty, 2) if qty else None
        direction = "favorable" if diff < 0 else ("adverse" if diff > 0 else "neutral")

        if favorable_only and direction != "favorable":
            continue

        importer, company_code, _ = _company_meta(lc, resolver)
        row = {
            "lc_id": lc.lc_id,
            "lc_number": lc.lc_number,
            "line_id": p.line_id,
            "item": p.item_code or p.product_code or p.product_name,
            "supplier": lc.supplier_name,
            "bank_name": norm_bank(lc.bank_name),
            "importer": importer,
            "company_code": company_code,
            "buyer": _buyer_label(lc),
            "vessel": _vessel_for_lc(lc),
            "lc_unit_price": unit,
            "current_lme": current_f,
            "difference": diff,
            "difference_pct": pct,
            "quantity_mt": qty or None,
            "impact_usd": impact_usd,
            "direction": direction,
            "baseline_lme": _f(p.baseline_lme),
            "lme_change_pct": _f(p.lme_change_percent),
        }
        if not _row_matches_filters(row, filters, resolver):
            continue
        if direction == "adverse":
            adverse_count += 1
        rows.append(row)

    rows.sort(key=lambda r: abs(r["impact_usd"] or 0), reverse=True)

    total_rows = len(rows)
    page_rows = rows
    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        page_rows = rows[offset:offset + page_size]

    return {
        "as_of": today.isoformat(),
        "filters": filters.as_dict() if filters else {},
        "totals": {
            "line_count": len(rows),
            "favorable_count": sum(1 for r in rows if r["direction"] == "favorable"),
            "adverse_count": adverse_count,
            "favorable_impact_usd": round(
                sum(abs(r["impact_usd"] or 0) for r in rows if r["direction"] == "favorable"), 2),
            "adverse_impact_usd": round(
                sum(r["impact_usd"] or 0 for r in rows if r["direction"] == "adverse"), 2),
        },
        "by_importer": _agg_dimension(rows, "company_code", "importer", {"impact_usd": "impact_usd"}),
        "by_buyer": _agg_dimension(rows, "buyer", "buyer", {"impact_usd": "impact_usd"}),
        "by_vessel": _agg_dimension(rows, "vessel", "vessel", {"impact_usd": "impact_usd"}),
        "rows": page_rows,
        "total_rows": total_rows,
        "page": page,
        "page_size": page_size,
    }


def penalty_and_risk_exposure(
    db: Session,
    *,
    gd_totals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Active monetary risks: demurrage accruing, GD penalties, high-severity alerts."""
    today = date.today()
    try:
        cfg = db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()
    except SQLAlchemyError:
        cfg = None
    per_day = float(cfg.per_day_charge or 0) if cfg else 0.0
    dem_currency = (cfg.currency if cfg else None) or "USD"

    demurrage_projected = 0.0
    demurrage_recorded = 0.0
    demurrage_count = 0
    try:
        bls = (
            db.query(BillOfLading)
            .options(joinedload(BillOfLading.shipment))
            .filter(
                BillOfLading.demurrage_start_date.isnot(None),
                BillOfLading.demurrage_cleared_date.is_(None),
            )
            .all()
        )
        for bl in bls:
            dem = compute_demurrage(bl, cfg, today)
            if dem["state"] == "ACCRUING":
                demurrage_count += 1
                recorded = dem.get("accrued_charge") or dem.get("total_amount")
                if recorded:
                    demurrage_recorded += float(recorded)
                elif per_day and dem.get("chargeable_days"):
                    demurrage_projected += per_day * int(dem["chargeable_days"])
    except SQLAlchemyError as exc:
        logger.warning("penalty_and_risk_exposure: demurrage query failed: %s", exc)

    high_alerts = active_alerts = 0
    try:
        high_alerts = (
            db.query(SystemAlert)
            .filter(SystemAlert.status == "ACTIVE", SystemAlert.severity == "HIGH")
            .count()
        )
        active_alerts = (
            db.query(SystemAlert)
            .filter(SystemAlert.status == "ACTIVE")
            .count()
        )
    except SQLAlchemyError as exc:
        logger.warning("penalty_and_risk_exposure: alerts query failed: %s", exc)

    duty_mtd = 0
    try:
        month_start = today.replace(day=1)
        duty_mtd = db.query(func.coalesce(func.sum(GoodsDeclaration.total_duties_pkr), 0)).filter(
            GoodsDeclaration.filing_date >= month_start
        ).scalar() or 0
    except SQLAlchemyError as exc:
        logger.warning("penalty_and_risk_exposure: duty MTD query failed: %s", exc)

    if gd_totals is None:
        gd_totals = gd_payables_forecast(db)["totals"]

    return {
        "as_of": today.isoformat(),
        "demurrage": {
            "accruing_count": demurrage_count,
            "recorded_total": round(demurrage_recorded, 2),
            "projected_accrual": round(demurrage_projected, 2) if per_day else None,
            "per_day_charge": per_day or None,
            "currency": dem_currency,
        },
        "gd_penalties_pkr": gd_totals.get("penalty_exposed_pkr", 0),
        "gd_duty_pipeline_pkr": gd_totals.get("potential_duty_pkr", 0),
        "duty_paid_mtd_pkr": float(duty_mtd),
        "active_alerts": active_alerts,
        "high_alerts": high_alerts,
    }


def _empty_lc_payables() -> Dict[str, Any]:
    return {
        "as_of": date.today().isoformat(),
        "horizon_days": 90,
        "totals": {"count": 0, "amount_pkr": 0.0, "missing_rate_count": 0, "buckets": _empty_buckets()},
        "by_bank": [],
        "rows": [],
    }


def _empty_bank_exposure() -> Dict[str, Any]:
    return {
        "as_of": date.today().isoformat(),
        "totals": {
            "offered_pkr": 0.0,
            "utilized_pkr": 0.0,
            "pending_pipeline_pkr": 0.0,
            "banks_over_projected_limit": 0,
        },
        "banks": [],
        "lc_utilization_rows": [],
        "by_importer": [],
        "by_buyer": [],
        "by_vessel": [],
    }


def _empty_gd_payables() -> Dict[str, Any]:
    return {
        "as_of": date.today().isoformat(),
        "totals": {
            "open_gd_count": 0,
            "potential_duty_pkr": 0.0,
            "penalty_exposed_pkr": 0.0,
            "duties_paid_pkr": 0.0,
            "due_within_30d_pkr": 0.0,
            "overdue_pkr": 0.0,
        },
        "forecast_by_week": [],
        "rows": [],
    }


def _empty_lme() -> Dict[str, Any]:
    return {
        "as_of": date.today().isoformat(),
        "totals": {
            "line_count": 0,
            "favorable_count": 0,
            "adverse_count": 0,
            "favorable_impact_usd": 0.0,
            "adverse_impact_usd": 0.0,
        },
        "rows": [],
    }


def _empty_risk(gd_totals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    gt = gd_totals or {}
    return {
        "as_of": date.today().isoformat(),
        "demurrage": {
            "accruing_count": 0,
            "recorded_total": 0.0,
            "projected_accrual": None,
            "per_day_charge": None,
            "currency": "USD",
        },
        "gd_penalties_pkr": gt.get("penalty_exposed_pkr", 0),
        "gd_duty_pipeline_pkr": gt.get("potential_duty_pkr", 0),
        "duty_paid_mtd_pkr": 0.0,
        "active_alerts": 0,
        "high_alerts": 0,
    }


def _safe_section(name: str, fn: Callable[[], T], default: T) -> tuple[T, Optional[str]]:
    try:
        return fn(), None
    except Exception as exc:
        logger.exception("roi_overview section %s failed", name)
        return default, f"{name}: {type(exc).__name__}: {exc}"


def roi_overview(db: Session, *, filters: Optional[RoiFilters] = None) -> Dict[str, Any]:
    """Single-call executive summary for the ROI dashboard."""
    errors: List[str] = []
    f = filters or RoiFilters()

    lc_pay, err = _safe_section(
        "lc_payables", lambda: lc_payables_forecast(db, filters=f), _empty_lc_payables())
    if err:
        errors.append(err)

    bank, err = _safe_section(
        "bank_exposure", lambda: bank_exposure_summary(db, filters=f), _empty_bank_exposure())
    if err:
        errors.append(err)

    gd, err = _safe_section(
        "gd_payables", lambda: gd_payables_forecast(db, filters=f), _empty_gd_payables())
    if err:
        errors.append(err)

    lme, err = _safe_section(
        "lme_opportunities", lambda: lme_opportunities(db, filters=f), _empty_lme())
    if err:
        errors.append(err)

    gd_totals = gd.get("totals") or {}
    risk, err = _safe_section(
        "risk_exposure",
        lambda: penalty_and_risk_exposure(db, gd_totals=gd_totals),
        _empty_risk(gd_totals),
    )
    if err:
        errors.append(err)

    dimensions = dimension_rollup(lc_pay, bank, gd, lme)

    lc_buckets = (lc_pay.get("totals") or {}).get("buckets") or _empty_buckets()
    bank_totals = bank.get("totals") or {}

    return {
        "as_of": date.today().isoformat(),
        "filters": f.as_dict(),
        "errors": errors,
        "kpis": {
            "lc_payables_pkr": (lc_pay.get("totals") or {}).get("amount_pkr", 0),
            "lc_payables_30d_pkr": lc_buckets.get("0_30", 0) + lc_buckets.get("overdue", 0),
            "bank_utilized_pkr": bank_totals.get("utilized_pkr", 0),
            "bank_available_pkr": round(
                float(bank_totals.get("offered_pkr") or 0) - float(bank_totals.get("utilized_pkr") or 0), 2),
            "gd_duty_pipeline_pkr": gd_totals.get("potential_duty_pkr", 0),
            "gd_penalty_exposed_pkr": gd_totals.get("penalty_exposed_pkr", 0),
            "lme_favorable_usd": (lme.get("totals") or {}).get("favorable_impact_usd", 0),
            "lme_adverse_usd": (lme.get("totals") or {}).get("adverse_impact_usd", 0),
            "demurrage_exposure": (risk.get("demurrage") or {}).get("recorded_total", 0),
            "duty_paid_mtd_pkr": risk.get("duty_paid_mtd_pkr", 0),
            "high_alerts": risk.get("high_alerts", 0),
        },
        "dimensions": dimensions,
        "lc_payables": lc_pay,
        "bank_exposure": bank,
        "gd_payables": gd,
        "lme_opportunities": lme,
        "risk": risk,
    }
