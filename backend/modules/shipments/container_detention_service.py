"""
Container detention — separate from coil demurrage.

Applies only to container BLs. Tracks free-time clock and the total detention
amount actually paid (entered manually, not calculated from a daily rate).

Ported from rehan/newchanges (backend/services/container_detention_service.py,
container_detention_report.py).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload

from models.database_models import BillOfLading, DemurrageConfig
from infrastructure.normalization.normalization_service import company_resolver, matches_company_code

_CONTAINER_RE = re.compile(
    r"\b(container|containers|cntr|cntrs|fcl|lcl|ctr|teu|feu|20\s*ft|40\s*ft|20'|40')\b",
    re.I,
)
_COIL_RE = re.compile(r"\b(coil|coils|reel|reels)\b", re.I)

_STATE_RANK = {"ACCRUING": 0, "AT_RISK": 1, "OK": 2, "ENDED": 3, "UNKNOWN": 4}


def _to_decimal(v):
    return Decimal(str(v)) if v is not None else None


def is_container_bl(bl, db: Session | None = None) -> bool:
    """True when this BL is for containerised cargo (not bulk coils)."""
    pt = (bl.package_type or "").strip()
    gd = (bl.goods_description or "").strip()
    marks = (bl.shipping_marks or "").strip()
    blob = " ".join(x for x in (pt, gd, marks) if x)

    if (_COIL_RE.search(pt) or _COIL_RE.search(gd)) and not _CONTAINER_RE.search(blob):
        return False
    if _CONTAINER_RE.search(blob):
        return True

    if db and bl.lc_id:
        from models.database_models import LCProduct

        rows = (
            db.query(LCProduct.num_containers)
            .filter(LCProduct.lc_id == bl.lc_id)
            .all()
        )
        if any(int(r[0] or 0) > 0 for r in rows):
            return True

    raw = getattr(bl, "raw_extracted_data", None) or {}
    if isinstance(raw, dict):
        for key in ("package_type", "goods_description", "shipping_marks"):
            val = str(raw.get(key) or "")
            if _CONTAINER_RE.search(val) and not (
                _COIL_RE.search(val) and not _CONTAINER_RE.search(val)
            ):
                return True
    return False


def resolve_bl_type(extracted: dict | None, bl, db: Session | None = None) -> str:
    """Resolve the authoritative COIL/CONTAINER classification for a BL.

    Prefers the AI extractor's own bl_type call (it reads the whole document, not just
    three summary fields) and falls back to the is_container_bl() regex heuristic when
    the AI didn't return a confident value (ambiguous document, or a manually-created
    BL with no extraction at all).
    """
    ai_value = str((extracted or {}).get("bl_type") or "").strip().upper()
    if ai_value in ("COIL", "CONTAINER"):
        return ai_value
    return "CONTAINER" if is_container_bl(bl, db) else "COIL"


def compute_container_detention(bl, config=None, today: date | None = None) -> dict:
    """Return detention status for a container BL."""
    today = today or date.today()
    warn_days = (config.warn_days if config and config.warn_days is not None else 3)
    currency = (
        bl.detention_currency
        or getattr(bl, "demurrage_currency", None)
        or (config.currency if config else "USD")
    )

    start = bl.detention_start_date or bl.demurrage_start_date or bl.bl_date or (bl.shipment.on_port_date if getattr(bl, "shipment", None) else None) or (bl.shipment.eta if getattr(bl, "shipment", None) else None)
    free_days = bl.detention_free_days if bl.detention_free_days is not None else (bl.free_days if bl.free_days is not None else (config.free_days if config and config.free_days is not None else 14))
    end = bl.detention_end_date
    paid = bl.detention_paid_date
    total = _to_decimal(getattr(bl, "detention_total_amount", None))
    total_float = float(total) if total is not None else None

    base = {
        "state": "UNKNOWN",
        "start_date": start.isoformat() if start else None,
        "free_days": free_days,
        "end_date": end.isoformat() if end else None,
        "paid_date": paid.isoformat() if paid else None,
        "total_amount": total_float,
        "currency": currency,
        "remarks": bl.detention_remarks,
        "last_free_day": None,
        "days_remaining": None,
        "chargeable_days": 0,
    }

    if not start or free_days is None:
        return base

    last_free_day = start + timedelta(days=int(free_days))
    base["last_free_day"] = last_free_day.isoformat()

    if end:
        chargeable = max(0, (end - last_free_day).days)
        base.update({
            "state": "ENDED",
            "days_remaining": (last_free_day - end).days,
            "chargeable_days": chargeable,
        })
        return base

    days_remaining = (last_free_day - today).days
    base["days_remaining"] = days_remaining

    if today > last_free_day:
        base.update({
            "state": "ACCRUING",
            "chargeable_days": (today - last_free_day).days,
        })
    elif days_remaining <= warn_days:
        base["state"] = "AT_RISK"
    else:
        base["state"] = "OK"

    return base


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _qmatch(needle, *haystacks):
    if not needle:
        return True
    u = needle.upper()
    return any(u in (h or "").upper() for h in haystacks if h)


def build_container_detention_report(db: Session, filters: dict | None = None) -> dict:
    """Container Detention report — one row per container BL."""
    f = filters or {}
    state_f = (f.get("state") or "").strip().upper() or None
    vessel_f = (f.get("vessel") or "").strip() or None
    pod_f = (f.get("port") or "").strip() or None
    importer_f = (f.get("importer") or "").strip() or None
    q = (f.get("q") or "").strip() or None
    date_from = _parse_date(f.get("date_from"))
    date_to = _parse_date(f.get("date_to"))
    open_only = str(f.get("open_only") or "").lower() in ("1", "true", "yes")

    cfg = db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()
    resolver = company_resolver(db)

    bls = (
        db.query(BillOfLading)
        .options(joinedload(BillOfLading.lc), joinedload(BillOfLading.shipment))
        .order_by(BillOfLading.bl_id.desc())
        .all()
    )

    vessels, ports, importers = set(), set(), set()
    rows = []
    total_recorded = defaultdict(float)
    totals: dict[str, Any] = {
        "bl_count": 0,
        "accruing_count": 0,
        "at_risk_count": 0,
        "ok_count": 0,
        "ended_count": 0,
        "unknown_count": 0,
        "unpaid_count": 0,
    }

    for bl in bls:
        if bl.bl_type != "CONTAINER":
            continue

        det = compute_container_detention(bl, cfg)
        lc = bl.lc
        importer = lc.importer_name if lc else None
        imp_res = resolver.resolve(importer)
        from infrastructure.normalization.normalization_service import norm_vessel
        vessel = norm_vessel(bl.vessel_name) or bl.vessel_name
        pod = bl.port_of_discharge

        if vessel:
            vessels.add(vessel)
        if pod:
            ports.add(pod)
        if importer:
            code = imp_res.get("short_code")
            if code:
                importers.add(code)

        st = det["state"]
        start_d = bl.detention_start_date

        if open_only and st == "ENDED":
            continue
        if state_f and st != state_f:
            continue
        if vessel_f and (vessel or "") != vessel_f:
            continue
        if pod_f and (pod or "") != pod_f:
            continue
        if importer_f and not matches_company_code(resolver, importer, importer_f):
            continue
        if date_from and (not start_d or start_d < date_from):
            continue
        if date_to and (not start_d or start_d > date_to):
            continue
        if q and not _qmatch(q, bl.bl_number, lc.lc_number if lc else None, vessel, pod, importer):
            continue

        cur = det.get("currency") or "USD"
        charge = float(det.get("total_amount") or 0)

        totals["bl_count"] += 1
        if charge:
            total_recorded[cur] += charge
        if st == "ACCRUING":
            totals["accruing_count"] += 1
        elif st == "AT_RISK":
            totals["at_risk_count"] += 1
        elif st == "OK":
            totals["ok_count"] += 1
        elif st == "ENDED":
            totals["ended_count"] += 1
            if not bl.detention_paid_date:
                totals["unpaid_count"] += 1
        else:
            totals["unknown_count"] += 1

        rows.append({
            "bl_id": bl.bl_id,
            "bl_number": bl.bl_number,
            "lc_id": bl.lc_id,
            "lc_number": lc.lc_number if lc else None,
            "shipment_id": bl.shipment_id,
            "importer_name": importer,
            "company_code": imp_res.get("short_code"),
            "company_matched": imp_res.get("matched"),
            "vessel_name": vessel,
            "port_of_discharge": pod,
            "package_count": bl.package_count,
            "package_type": bl.package_type,
            "detention": det,
        })

    rows.sort(key=lambda r: (
        _STATE_RANK.get(r["detention"]["state"], 9),
        r["detention"].get("days_remaining")
        if r["detention"].get("days_remaining") is not None else 999,
        -(r["detention"].get("chargeable_days") or 0),
        r["bl_number"] or "",
    ))

    totals["total_recorded"] = dict(total_recorded)

    return {
        "options": {
            "vessels": sorted(vessels),
            "ports": sorted(ports),
            "importers": sorted(importers),
            "states": ["ACCRUING", "AT_RISK", "OK", "ENDED", "UNKNOWN"],
        },
        "config": {
            "free_days": cfg.free_days if cfg else None,
            "currency": cfg.currency if cfg else "USD",
            "warn_days": cfg.warn_days if cfg else 3,
        },
        "totals": totals,
        "rows": rows,
    }
