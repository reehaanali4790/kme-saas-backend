"""Unified My Work feed — merges action center, expiries, and cost-at-risk."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from modules.alerts import action_center_service as ac_svc
from modules.shipments import demurrage_service as dem_svc

# Cost-relevant alert types surfaced prominently
_COST_ALERT_TYPES = frozenset({
    "GD_FILING_DUE", "GD_FILING_OVERDUE", "GD_LATE_FILED",
    "DEMURRAGE", "DEMURRAGE_ACCRUING", "DEMURRAGE_AT_RISK", "CONTAINER_LFD",
    "FI_EXPIRY", "MISSING_DOCUMENT", "ARRIVAL_DOCS_MISSING",
    "SRO_QUOTA_EXCEEDED", "SRO_QUOTA_90",
})

_TONE_RANK = {"overdue": 0, "expired": 0, "critical": 1, "due_soon": 2, "due": 2, "upcoming": 3, "ok": 4, "unknown": 5}
_SEV_RANK = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _unified_tone(item: dict) -> str:
    tone = (item.get("tone") or "").lower()
    days = item.get("days_remaining")
    if tone in ("expired", "overdue"):
        return "overdue"
    if days is not None and days <= 0:
        return "overdue"
    if tone == "critical" or (days is not None and days <= 3):
        return "critical"
    if tone in ("due", "upcoming") or (days is not None and days <= 10):
        return "due_soon"
    return "ok"


def _cost_for_shipment(db: Session, shipment_id: Optional[int]) -> Optional[dict]:
    if not shipment_id:
        return None
    try:
        bls = dem_svc.at_risk_bls(db)
        for row in bls.get("items", []):
            dem = row.get("demurrage") or {}
            sid = row.get("shipment_id")
            if sid != shipment_id:
                from models.database_models import BillOfLading
                bl = db.query(BillOfLading).filter(BillOfLading.bl_id == row.get("bl_id")).first()
                sid = bl.shipment_id if bl else None
            if sid == shipment_id:
                amt = dem.get("accrued_charge") or dem.get("estimated_charge")
                if amt:
                    return {
                        "amount": float(amt),
                        "currency": dem.get("currency") or "USD",
                        "label": "Demurrage at risk",
                    }
    except Exception:
        pass
    return None


def _enrich_item(db: Session, item: dict) -> dict:
    tone = _unified_tone(item)
    sev = (item.get("severity") or "MEDIUM").upper()
    if tone == "overdue":
        sev = "CRITICAL"
    elif tone == "critical" and sev == "LOW":
        sev = "HIGH"

    cost = None
    cat = item.get("category") or ""
    alert_type = (item.get("alert_type") or item.get("title") or "").upper()
    if cat in ("CUSTOMS", "DOCUMENT", "LC_DATES") or any(t in alert_type for t in _COST_ALERT_TYPES):
        cost = _cost_for_shipment(db, item.get("shipment_id"))

    href = item.get("href") or "/my-work"
    if item.get("shipment_id") and "/shipment" in href and "tab=workflow" not in href:
        href = f"/shipment?id={item['shipment_id']}&tab=workflow"

    return {
        **item,
        "severity": sev,
        "tone": tone,
        "cost_impact": cost,
        "href": href,
        "label": item.get("title") or item.get("message") or "Action required",
        "category": cat or "DOCUMENT",
    }


def list_my_work(
    db: Session,
    *,
    scope: str = "today",
    limit: int = 100,
) -> dict:
    """Merge action-center items with unified severity and cost labels."""
    tab_map = {"today": "today", "week": "ALL", "all": "ALL"}
    tab = tab_map.get((scope or "today").lower(), "today")
    raw = ac_svc.list_action_center(db, tab=tab, limit=limit * 2)

    items = [_enrich_item(db, i) for i in raw.get("items", [])]

    if scope.lower() == "week":
        items = [
            i for i in items
            if i.get("days_remaining") is None or i["days_remaining"] <= 7
        ]

    items.sort(key=lambda i: (
        _TONE_RANK.get(i.get("tone") or "unknown", 5),
        _SEV_RANK.get(i.get("severity") or "LOW", 2),
        i.get("days_remaining") if i.get("days_remaining") is not None else 999,
    ))
    items = items[:limit]

    cost_total = sum(
        (i["cost_impact"]["amount"] for i in items if i.get("cost_impact") and i["cost_impact"].get("amount")),
        0.0,
    )

    return {
        "today": date.today().isoformat(),
        "scope": scope,
        "counts": raw.get("counts", {}),
        "cost_at_risk_total": round(cost_total, 2),
        "cost_currency": "USD",
        "items": items,
    }
