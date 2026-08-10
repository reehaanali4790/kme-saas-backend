"""
Alert Engine API — scan, list, acknowledge, dismiss operational alerts.
Feeds the dashboard "Needs Attention" panel and WhatsApp (both critical
operational alerts and any not-yet-delivered LME price-alert PDFs).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.permissions import require_permission
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.alerts import engine_service as svc

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/alert-engine", tags=["Alert Engine"])
_require_admin = require_permission("manage_users")


@router.post("/scan")
def run_scan(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
    send_whatsapp: bool = Query(True, description="Also flush pending alerts to WhatsApp"),
):
    """
    Run the alert engine now — this is what the 15-minute scheduler
    (main.py) calls automatically; this endpoint is the manual/on-demand
    equivalent. By default also flushes both WhatsApp queues: critical
    operational alerts and any not-yet-sent LME price-alert PDFs.
    """
    return svc.run_scan(db, send_whatsapp)


@router.post("/send-whatsapp")
def send_whatsapp_now(db: Session = Depends(get_tenant_db),
                      current_user: User = Depends(_require_admin)):
    """Manually flush both WhatsApp queues now (operational + price alerts) — also a retry path."""
    return svc.send_whatsapp_now(db)


@router.get("/")
def list_alerts(
    status: str = Query("ACTIVE"),
    severity: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return svc.list_alerts(db, status, severity, alert_type, limit)


@router.get("/stats")
def alert_stats(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    return svc.alert_stats(db)


@router.post("/{alert_id}/acknowledge")
def acknowledge(alert_id: int, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(get_current_user)):
    svc.acknowledge(db, alert_id, current_user.user_id)
    return {"success": True, "alert_id": alert_id}


@router.post("/{alert_id}/dismiss")
def dismiss(alert_id: int, db: Session = Depends(get_tenant_db),
            current_user: User = Depends(get_current_user)):
    svc.dismiss(db, alert_id)
    return {"success": True, "alert_id": alert_id}
