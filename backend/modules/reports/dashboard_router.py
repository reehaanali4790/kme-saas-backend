"""
Dashboard summary API — one efficient call returning all KPIs, breakdowns,
deadlines, top suppliers, recent shipments and alert highlights for the
operations command center.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from modules.auth.dependencies import get_current_user
from models.database_models import User
from modules.reports import dashboard_service as svc
from modules.reports import dashboard_v2_service as svc_v2

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("/summary")
def summary(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    return svc.summary(db)


@router.get("/arrivals")
def arrivals(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    """Upcoming vessel arrivals — the primary operations panel.
    Shipments with an ETA, not yet completed, with the details the import team needs."""
    return svc.arrivals(db)


@router.get("/v2/summary")
def v2_summary(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    """Executive dashboard — KPIs, charts and tables for Dashboard 2."""
    import orjson
    from core.redis import redis_cache

    cache_key = "lme:dashboard:v2:summary"
    cached = redis_cache.get(cache_key)
    if cached:
        try:
            return orjson.loads(cached)
        except Exception:
            pass

    result = svc_v2.v2_summary(db)
    try:
        redis_cache.set(cache_key, orjson.dumps(result).decode("utf-8"), ex=300)
    except Exception:
        pass
    return result
