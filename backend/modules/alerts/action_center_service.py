"""Unified Action Center — aggregates operational alerts, deadlines, and LME price alerts."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from models.database_models import PriceAlert, LCMaster, LCProduct, SystemAlert
from modules.alerts import engine_service as eng_svc
from modules.alerts import expiries_service as exp_svc


CATEGORY_MAP = {
    "DOCUMENT": {"MISSING_DOCUMENT", "ARRIVAL_DOCS_MISSING", "POST_SHIP_DOCS_MISSING", "DA_DOC_ARRIVAL"},
    "CUSTOMS": {"GD_FILING_DUE", "GD_FILING_OVERDUE", "GD_LATE_FILED", "EX_BOND_EXCEEDED", "INTO_BOND"},
    "LC_DATES": {"LC_EXPIRING", "LAST_SHIP_DATE", "FI_EXPIRY", "SHORT_SHIPMENT"},
    "LME": set(),
}

EXPIRY_CATEGORY = {
    "LC_EXPIRY": "LC_DATES",
    "LAST_SHIP": "LC_DATES",
    "FYI": "LC_DATES",
    "VESSEL_ETA": "DOCUMENT",
    "GD_FILING": "CUSTOMS",
    "INTO_BOND": "CUSTOMS",
    "MATURITY": "LC_DATES",
    "SRO_QUOTA": "LC_DATES",
    "CONTRACT": "LC_DATES",
    "BANK_LIMIT": "LC_DATES",
}


def _severity_rank(sev: str) -> int:
    return {"HIGH": 0, "CRITICAL": 0, "MEDIUM": 1, "LOW": 2}.get((sev or "").upper(), 3)


def _tone_rank(tone: str) -> int:
    return {"expired": 0, "overdue": 0, "critical": 1, "due": 2, "upcoming": 3, "ok": 4, "unknown": 5}.get(tone, 6)


def _op_category(alert_type: str) -> str:
    at = (alert_type or "").upper()
    for cat, types in CATEGORY_MAP.items():
        if at in types:
            return cat
    if "DEMURRAGE" in at or "DISCREPANCY" in at or "DUTY" in at:
        return "CUSTOMS" if "DUTY" in at else "DOCUMENT"
    return "DOCUMENT"


def _op_href(a: SystemAlert) -> Optional[str]:
    if a.shipment_id:
        return f"/shipment?id={a.shipment_id}"
    if a.lc_id:
        return f"/lc-detail?id={a.lc_id}"
    return "/alerts-center"


def _expiry_to_item(exp: dict) -> dict:
    cat = EXPIRY_CATEGORY.get(exp["doc_type"], "LC_DATES")
    sev = "HIGH" if exp["tone"] in ("expired", "critical") else ("MEDIUM" if exp["tone"] == "upcoming" else "LOW")
    return {
        "id": f"expiry-{exp['doc_type']}-{exp.get('entity_id')}-{exp['expiry_date']}",
        "source": "deadline",
        "category": cat,
        "severity": sev,
        "title": exp["title"],
        "message": exp.get("note") or exp.get("doc_label"),
        "due_date": exp["expiry_date"],
        "days_remaining": exp["days_remaining"],
        "tone": exp["tone"],
        "lc_id": exp.get("lc_id"),
        "lc_number": exp.get("lc_number"),
        "shipment_id": exp.get("shipment_id"),
        "href": exp.get("href") or "/expiries",
        "status": "ACTIVE",
    }


def _price_to_item(alert: PriceAlert, lc: LCMaster, product: LCProduct) -> dict:
    unviewed = not alert.viewed
    sev = "HIGH" if (alert.priority or "").upper() == "HIGH" else "MEDIUM"
    return {
        "id": f"price-{alert.alert_id}",
        "source": "lme_price",
        "category": "LME",
        "severity": sev if unviewed else "LOW",
        "title": f"LME price change — LC {lc.lc_number}",
        "message": f"{product.product_code} {product.origin}: {alert.alert_type} "
                   f"({float(alert.difference_percent or 0):+.1f}%)",
        "due_date": alert.alert_date.isoformat() if alert.alert_date else None,
        "days_remaining": lc.days_remaining,
        "tone": "critical" if unviewed and sev == "HIGH" else "upcoming",
        "lc_id": lc.lc_id,
        "lc_number": lc.lc_number,
        "shipment_id": None,
        "href": "/alerts",
        "status": "DONE" if alert.viewed else "ACTIVE",
        "alert_id": alert.alert_id,
    }


def _op_to_item(a: SystemAlert, lc_number: Optional[str]) -> dict:
    return {
        "id": f"op-{a.alert_id}",
        "source": "operational",
        "category": _op_category(a.alert_type),
        "severity": a.severity or "MEDIUM",
        "title": a.title,
        "message": a.message,
        "due_date": a.created_at.date().isoformat() if a.created_at else None,
        "days_remaining": None,
        "tone": "critical" if (a.severity or "").upper() == "HIGH" else "upcoming",
        "lc_id": a.lc_id,
        "lc_number": lc_number,
        "shipment_id": a.shipment_id,
        "href": _op_href(a),
        "status": a.status,
        "alert_id": a.alert_id,
    }


def list_action_center(
    db: Session,
    *,
    tab: str = "today",
    limit: int = 100,
) -> dict:
    today = date.today()
    items: list[dict] = []

    op_data = eng_svc.list_alerts(db, "ACTIVE", None, None, 500)
    for row in op_data.get("items", []):
        items.append({
            "id": f"op-{row['alert_id']}",
            "source": "operational",
            "category": _op_category(row.get("alert_type", "")),
            "severity": row.get("severity") or "MEDIUM",
            "title": row.get("title"),
            "message": row.get("message"),
            "due_date": (row.get("created_at") or "")[:10] or None,
            "days_remaining": None,
            "tone": "critical" if (row.get("severity") or "").upper() == "HIGH" else "upcoming",
            "lc_id": row.get("lc_id"),
            "lc_number": row.get("lc_number"),
            "shipment_id": row.get("shipment_id"),
            "href": f"/shipment?id={row['shipment_id']}" if row.get("shipment_id") else (
                f"/lc-detail?id={row['lc_id']}" if row.get("lc_id") else "/alerts-center"
            ),
            "status": row.get("status", "ACTIVE"),
            "alert_id": row.get("alert_id"),
        })

    exp_data = exp_svc.list_expiries(db, None, None, None, days_max=30, include_ok=False)
    for exp in exp_data.get("items", []):
        items.append(_expiry_to_item(exp))

    price_rows = (
        db.query(PriceAlert, LCMaster, LCProduct)
        .join(LCProduct, PriceAlert.line_id == LCProduct.line_id)
        .join(LCMaster, PriceAlert.lc_id == LCMaster.lc_id)
        .filter(PriceAlert.viewed.is_(False))
        .order_by(PriceAlert.alert_date.desc())
        .limit(50)
        .all()
    )
    for alert, lc, product in price_rows:
        items.append(_price_to_item(alert, lc, product))

    tab_upper = (tab or "today").upper()
    if tab_upper == "TODAY":
        filtered = [
            i for i in items
            if i["status"] == "ACTIVE" and (
                i.get("days_remaining") is None
                or i["days_remaining"] <= 3
                or i.get("tone") in ("expired", "critical", "overdue")
                or (i.get("severity") or "").upper() == "HIGH"
            )
        ]
    elif tab_upper == "DOCUMENT":
        filtered = [i for i in items if i["category"] == "DOCUMENT" and i["status"] == "ACTIVE"]
    elif tab_upper == "CUSTOMS":
        filtered = [i for i in items if i["category"] == "CUSTOMS" and i["status"] == "ACTIVE"]
    elif tab_upper == "LC_DATES":
        filtered = [i for i in items if i["category"] == "LC_DATES" and i["status"] == "ACTIVE"]
    elif tab_upper == "LME":
        filtered = [i for i in items if i["category"] == "LME"]
    elif tab_upper == "DONE":
        filtered = [i for i in items if i["status"] != "ACTIVE"]
    else:
        filtered = [i for i in items if i["status"] == "ACTIVE"]

    filtered.sort(key=lambda i: (
        _tone_rank(i.get("tone") or "unknown"),
        _severity_rank(i.get("severity") or "LOW"),
        i.get("days_remaining") if i.get("days_remaining") is not None else 999,
    ))

    filtered = filtered[:limit]

    counts = {
        "today": len([i for i in items if i["status"] == "ACTIVE" and (
            i.get("days_remaining") is None or i["days_remaining"] <= 3
            or i.get("tone") in ("expired", "critical", "overdue")
        )]),
        "active_total": len([i for i in items if i["status"] == "ACTIVE"]),
        "document": len([i for i in items if i["category"] == "DOCUMENT" and i["status"] == "ACTIVE"]),
        "customs": len([i for i in items if i["category"] == "CUSTOMS" and i["status"] == "ACTIVE"]),
        "lc_dates": len([i for i in items if i["category"] == "LC_DATES" and i["status"] == "ACTIVE"]),
        "lme": len([i for i in items if i["category"] == "LME" and i["status"] == "ACTIVE"]),
    }

    return {
        "today": today.isoformat(),
        "tab": tab,
        "counts": counts,
        "items": filtered,
    }
