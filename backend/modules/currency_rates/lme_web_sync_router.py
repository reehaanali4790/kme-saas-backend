"""
thehelpers.pk LME bulletin auto-sync — admin manual trigger, latest-rates readout,
and execution history. Separate router from lme_rates_router.py (the FormulaEngine
matrix for manually uploaded bulletins), sharing the same /api/lme-rates prefix.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.permissions import require_permission
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.currency_rates import lme_web_rates_service as svc
from integrations.thehelpers.thehelpers_lme_service import sync_web_bulletin
from integrations.thehelpers.thehelpers_bulletin_crawler import crawl_and_import_bulletins

router = APIRouter(prefix="/api/lme-rates", tags=["LME Rates - Web Sync"])
logger = logging.getLogger("uvicorn")

_can_manage = require_permission("manage_users")


def _whatsapp_rates_outcome(crawl_result: dict) -> dict:
    """
    `success: true` on this endpoint only ever reflects the WEB-panel sync — it says
    nothing about whether a rates PDF actually reached WhatsApp, since that depends on
    the separate crawler job finding an importable bulletin. Surface that explicitly so
    "Sync clicked, panel updated" can't be mistaken for "alert sent" when the crawler
    found nothing new or the WhatsApp send itself failed.
    """
    for r in crawl_result.get("results", []):
        wa = r.get("whatsapp_rates_report")
        if wa:
            return {"dispatched": wa.get("messages_sent", 0) > 0, "detail": wa}
    return {"dispatched": False, "detail": {"skipped_reason": "no new bulletin imported this run"}}


@router.post("/sync-web")
def sync_web(current_user: User = Depends(_can_manage)):
    """Manually trigger BOTH thehelpers.pk jobs (admin only): the WEB-source sync (feeds
    the "Auto-Synced Reference Rates" panel) and the bulletin crawler (imports any new
    bulletin through the manual-upload pipeline, feeding the Rate Matrix/cards directly)
    - one click keeps both in sync instead of the crawler being schedule-only. Always
    returns 200 - a failed run is a normal, loggable outcome (site down, layout changed),
    not a server error; existing bulletin data is left untouched either way."""
    result = sync_web_bulletin(trigger='MANUAL', triggered_by=int(current_user.user_id))
    crawl_result = crawl_and_import_bulletins(trigger='MANUAL')
    return {
        "success": result.get("status") == "SUCCESS",
        **result,
        "bulletin_crawl": crawl_result,
        "whatsapp_rates_report": _whatsapp_rates_outcome(crawl_result),
    }


@router.get("/web-latest")
def web_latest(
    group: Optional[str] = Query(None),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Latest auto-synced (source='WEB') reference rates, optionally filtered by product group."""
    return svc.get_latest_web_rates(db, group)


@router.get("/sync-history")
def sync_history(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_manage),
):
    """Execution history for the web-sync job (admin only)."""
    return {"data": svc.get_sync_history(db, limit)}
