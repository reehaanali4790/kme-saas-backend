"""Business logic for the operational alert engine API, extracted from
modules/alerts/engine_router.py as part of the Phase 4 module rollout.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError
from models.database_models import SystemAlert, LCMaster
from infrastructure.alerts.alert_engine import scan

logger = logging.getLogger("uvicorn")

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def to_dict(a: SystemAlert, lc_number=None) -> dict:
    return {
        "alert_id": a.alert_id, "alert_type": a.alert_type, "severity": a.severity,
        "title": a.title, "message": a.message,
        "entity_type": a.entity_type, "entity_id": a.entity_id,
        "lc_id": a.lc_id, "shipment_id": a.shipment_id, "lc_number": lc_number,
        "status": a.status, "whatsapp_sent": a.whatsapp_sent,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def run_scan(db: Session, send_whatsapp: bool) -> dict:
    """
    Run the alert engine now — called both by the 15-minute scheduler
    (main.py) and the manual "run scan" endpoint. When send_whatsapp is set,
    also flushes all THREE pending WhatsApp queues: operational (critical
    SystemAlerts, text), LME price alerts (PDF), and the LME rates report
    (PDF). Price alerts and the rates report are otherwise only sent at
    bulletin-upload time, so folding them into this periodic job is what
    gives them an automatic retry if that first attempt ever fails (template
    not yet approved, a transient network error, etc.) — without it, a failed
    send would sit stuck until the next bulletin or a manual click. Each send
    is isolated in its own try/except so a failure in one never blocks or
    masks the others.
    """
    result = scan(db)
    if send_whatsapp:
        result["whatsapp"] = _dispatch_whatsapp(db)
    return result


def send_whatsapp_now(db: Session) -> dict:
    """Flush all pending WhatsApp queues now (operational + price alerts + rates report)."""
    return _dispatch_whatsapp(db)


def _dispatch_whatsapp(db: Session) -> dict:
    from infrastructure.whatsapp.whatsapp_service import (
        send_system_alerts, send_pending_alerts, send_rates_report,
    )

    out: dict = {}
    try:
        out["operational"] = send_system_alerts(db)
    except Exception as e:
        logger.warning(f"WhatsApp operational-alert dispatch failed (scan still succeeded): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        out["operational"] = {"error": str(e)}

    try:
        out["price_alerts"] = send_pending_alerts(db)
    except Exception as e:
        logger.warning(f"WhatsApp price-alert dispatch failed (scan still succeeded): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        out["price_alerts"] = {"error": str(e)}

    try:
        out["rates_report"] = send_rates_report(db)
    except Exception as e:
        logger.warning(f"WhatsApp rates-report dispatch failed (scan still succeeded): {e}")
        try:
            db.rollback()
        except Exception:
            pass
        out["rates_report"] = {"error": str(e)}

    return out


def list_alerts(db: Session, status: str, severity: Optional[str],
                alert_type: Optional[str], limit: int) -> dict:
    q = db.query(SystemAlert)
    if status and status != "ALL":
        q = q.filter(SystemAlert.status == status.upper())
    if severity:
        q = q.filter(SystemAlert.severity == severity.upper())
    if alert_type:
        q = q.filter(SystemAlert.alert_type == alert_type.upper())
    alerts = q.all()
    # sort by severity then recency
    alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a.severity, 9),
                               -(a.created_at.timestamp() if a.created_at else 0)))
    alerts = alerts[:limit]

    # batch-fetch LC numbers
    lc_ids = {a.lc_id for a in alerts if a.lc_id}
    lc_map = {}
    if lc_ids:
        for lc in db.query(LCMaster.lc_id, LCMaster.lc_number).filter(LCMaster.lc_id.in_(lc_ids)).all():
            lc_map[lc.lc_id] = lc.lc_number
    return {"total": len(alerts), "items": [to_dict(a, lc_map.get(a.lc_id)) for a in alerts]}


def alert_stats(db: Session) -> dict:
    base = db.query(SystemAlert).filter(SystemAlert.status == "ACTIVE")
    total = base.count()
    by_sev = {s: base.filter(SystemAlert.severity == s).count() for s in ("HIGH", "MEDIUM", "LOW")}
    by_type = {t: c for t, c in db.query(SystemAlert.alert_type, func.count(SystemAlert.alert_id))
                   .filter(SystemAlert.status == "ACTIVE")
                   .group_by(SystemAlert.alert_type).all()}
    return {"active_total": total, "by_severity": by_sev, "by_type": by_type}


def get_alert_or_404(db: Session, alert_id: int) -> SystemAlert:
    a = db.query(SystemAlert).filter(SystemAlert.alert_id == alert_id).first()
    if not a:
        raise NotFoundError("Alert not found")
    return a


def acknowledge(db: Session, alert_id: int, user_id: int):
    a = get_alert_or_404(db, alert_id)
    a.status = "ACKNOWLEDGED"
    a.acknowledged_by = user_id
    a.acknowledged_at = datetime.utcnow()
    db.commit()


def dismiss(db: Session, alert_id: int):
    a = get_alert_or_404(db, alert_id)
    a.status = "DISMISSED"
    db.commit()
