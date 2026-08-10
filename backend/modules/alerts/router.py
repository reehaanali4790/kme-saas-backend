"""
LME Monitoring System - Alert Management API Endpoints
Version: 2.0
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.permissions import require_permission
from core.schemas import PaginatedResponse, PaginationParams
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.alerts import service as svc
from modules.alerts.schemas import (
    AlertResponse, TakeActionRequest, WhatsAppTestRequest,
    WhatsAppConfigOut, WhatsAppConfigUpsert,
)

router = APIRouter(prefix="/api/alerts", tags=["Alerts"])

_can_take_action = require_permission("reopen_lc")
_require_admin = require_permission("manage_users")


@router.get("/list", response_model=PaginatedResponse[AlertResponse])
def list_alerts(
    alert_type: Optional[str] = Query(None),
    viewed: Optional[bool] = Query(None),
    priority: Optional[str] = Query(None),
    origin: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    product_code: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    params: PaginationParams = Depends(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """List alerts with filters: type, viewed, priority, origin, product, date range, search."""
    return svc.list_alerts(db, alert_type, viewed, priority, origin, product_code,
                           date_from, date_to, params, search)


@router.get("/savings-opportunities", response_model=List[AlertResponse])
def get_savings_opportunities(
    min_impact: Optional[float] = Query(None, description="Minimum dollar impact"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get all savings opportunities (price decreases)"""
    return svc.savings_opportunities(db, min_impact)


@router.post("/{alert_id}/mark-viewed")
def mark_alert_viewed(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Mark an alert as viewed"""
    svc.mark_viewed(db, alert_id, current_user.user_id)
    return {"message": "Alert marked as viewed"}


@router.post("/{alert_id}/take-action")
def take_action_on_alert(
    alert_id: int,
    action_data: TakeActionRequest,
    current_user: User = Depends(_can_take_action),
    db: Session = Depends(get_tenant_db)
):
    """Take action on an alert (manager only)"""
    svc.take_action(db, alert_id, action_data, current_user.user_id)
    return {
        "message": "Action recorded successfully",
        "action": action_data.action,
        "alert_id": alert_id
    }


@router.get("/stats/summary")
def get_alert_statistics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get alert statistics"""
    return {"success": True, "stats": svc.alert_statistics(db)}


@router.post("/dismiss-all")
def dismiss_all_alerts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Mark all unread alerts as viewed"""
    dismissed = svc.dismiss_all(db, current_user.user_id)
    return {"success": True, "dismissed": dismissed}


@router.get("/{alert_id}")
def get_alert_detail(
    alert_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get detailed alert information"""
    return svc.alert_detail(db, alert_id)


# --------------------------------------------------------------------------- #
# WhatsApp delivery
# --------------------------------------------------------------------------- #

@router.get("/whatsapp/status")
def whatsapp_status(current_user: User = Depends(_require_admin)):
    """Report whether WhatsApp sending is configured and ready."""
    from infrastructure.whatsapp import whatsapp_service
    from config.settings import settings
    pdf_template = whatsapp_service.get_pdf_template_status()
    return {
        "enabled": settings.WHATSAPP_ENABLED,
        "configured": whatsapp_service.is_configured(),
        "mode": "template" if settings.WHATSAPP_USE_TEMPLATE else "free-form text",
        "phone_number_id_set": bool(settings.WHATSAPP_PHONE_NUMBER_ID),
        "token_set": bool(settings.WHATSAPP_TOKEN),
        "template_name": settings.WHATSAPP_TEMPLATE_NAME,
        "pdf_template_name": settings.WHATSAPP_PDF_TEMPLATE_NAME,
        "pdf_template_exists": pdf_template.get("exists", False),
        "pdf_template_status": pdf_template.get("status"),
    }


@router.post("/whatsapp/provision-template")
def whatsapp_provision_template(current_user: User = Depends(_require_admin)):
    """
    Submit the branded-PDF WhatsApp template to Meta for approval, if it
    doesn't already exist. Safe to call repeatedly (no-op once it exists).
    """
    from infrastructure.whatsapp import whatsapp_service
    return whatsapp_service.ensure_price_alert_pdf_template()


@router.post("/whatsapp/test")
def whatsapp_test(
    payload: WhatsAppTestRequest,
    current_user: User = Depends(_require_admin),
):
    """Send a one-off test message to verify credentials end-to-end."""
    from infrastructure.whatsapp import whatsapp_service
    result = whatsapp_service.send_test_message(payload.to, payload.country_code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result.get("error", "send failed"))
    return {"sent": True, "message_id": result.get("message_id")}


@router.post("/whatsapp/send-pending")
def whatsapp_send_pending(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    """Dispatch all not-yet-sent alerts to recipients (also works as a retry)."""
    from infrastructure.whatsapp import whatsapp_service
    return whatsapp_service.send_pending_alerts(db)


@router.post("/{alert_id}/send-whatsapp")
def whatsapp_resend(
    alert_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    """Manually (re)send a single alert to all active recipients who want it."""
    from infrastructure.whatsapp import whatsapp_service
    result = whatsapp_service.send_single_alert(db, alert_id)
    if result.get("skipped_reason") == "alert not found":
        raise HTTPException(status_code=404, detail="Alert not found")
    return result


@router.get("/whatsapp/config", response_model=Optional[WhatsAppConfigOut])
def whatsapp_get_config(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    """Return the caller's WhatsApp recipient row, or null if not registered yet."""
    from models.database_models import WhatsAppConfig
    return (
        db.query(WhatsAppConfig)
        .filter(WhatsAppConfig.user_id == current_user.user_id)
        .first()
    )


@router.put("/whatsapp/config", response_model=WhatsAppConfigOut)
def whatsapp_upsert_config(
    payload: WhatsAppConfigUpsert,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    """
    Create or update the caller's WhatsApp recipient row. Always receives ALL
    alert types (price + operational) — per-type filtering isn't exposed yet.
    """
    from models.database_models import WhatsAppConfig

    conflict = (
        db.query(WhatsAppConfig)
        .filter(
            WhatsAppConfig.phone_number == payload.phone_number,
            WhatsAppConfig.user_id != current_user.user_id,
        )
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="This phone number is already registered to another WhatsApp recipient.",
        )

    cfg = (
        db.query(WhatsAppConfig)
        .filter(WhatsAppConfig.user_id == current_user.user_id)
        .first()
    )
    if not cfg:
        cfg = WhatsAppConfig(user_id=current_user.user_id, alert_type="ALL")
        db.add(cfg)

    cfg.recipient_name = payload.recipient_name
    cfg.phone_number = payload.phone_number
    cfg.country_code = payload.country_code
    cfg.active = payload.active
    cfg.alert_type = "ALL"

    db.commit()
    db.refresh(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Delivery-status webhook — called directly by Meta, deliberately NOT behind
# our own login. Meta proves it owns the setup via the verify token (GET, one
# time) and there's nothing sensitive returned (POST only updates our own
# delivery-status column, never reads/exposes data).
# --------------------------------------------------------------------------- #

@router.get("/whatsapp/webhook")
def whatsapp_webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """One-time handshake Meta performs when you save the webhook URL in WhatsApp Manager."""
    from fastapi.responses import PlainTextResponse
    from config.settings import settings

    if (hub_mode == "subscribe"
            and settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
            and hub_verify_token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request, db: Session = Depends(get_tenant_db)):
    """Meta calls this with delivery-status updates (sent/delivered/read/failed)."""
    from infrastructure.whatsapp import whatsapp_service
    payload = await request.json()
    updated = whatsapp_service.process_delivery_status_webhook(payload, db)
    return {"received": True, "alerts_updated": updated}
