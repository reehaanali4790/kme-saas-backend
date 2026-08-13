"""Unified Action Center API."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.alerts import action_center_service as svc

router = APIRouter(prefix="/api/action-center", tags=["Action Center"])


@router.get("/")
def get_action_center(
    tab: str = Query("today", description="today | documents | customs | lc_dates | lme | done"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    tab_map = {
        "today": "today",
        "documents": "document",
        "customs": "customs",
        "lc_dates": "lc_dates",
        "lc-dates": "lc_dates",
        "lme": "lme",
        "lme_prices": "lme",
        "done": "done",
    }
    normalized = tab_map.get(tab.lower(), tab.lower())
    return svc.list_action_center(db, tab=normalized, limit=limit)
