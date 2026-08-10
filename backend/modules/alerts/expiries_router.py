"""Unified document / milestone expiries for LC, FYI, vessels, GD, contracts, etc."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.alerts import expiries_service as svc

router = APIRouter(prefix="/api/expiries", tags=["Expiries"])


@router.get("/")
def list_expiries(
    doc_type: Optional[str] = Query(None, description="Comma-separated DOC_TYPES"),
    tone: Optional[str] = Query(None, description="expired,critical,upcoming,ok"),
    search: Optional[str] = Query(None),
    days_max: Optional[int] = Query(None, description="Only items with days_remaining <= N"),
    include_ok: bool = Query(True, description="Include green (>30 days) items"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Aggregate expiry / deadline dates across documents and milestones."""
    return svc.list_expiries(db, doc_type, tone, search, days_max, include_ok)
