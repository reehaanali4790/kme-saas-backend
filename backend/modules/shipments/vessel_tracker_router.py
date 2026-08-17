"""Authenticated backend proxy for vessel tracking (VesselAPI + AISStream fallback)."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.plan_limits import require_plan_feature
from core.tenant import TenantContext
from modules.auth.dependencies import get_current_user
from models.database_models import User
from integrations.vessel_api.vessel_api_service import VesselAPIError, track_vessel_with_fallback

router = APIRouter(prefix="/api/vessel-tracker", tags=["Vessel Tracker"])


@router.get("/track")
async def track_vessel(
    name: str = Query(..., min_length=2, max_length=200),
    current_user: User = Depends(get_current_user),
    _tenant: TenantContext = Depends(require_plan_feature("vessel_tracking")),
):
    """Search VesselAPI first; if not found or failed, fall back to AISStream live AIS."""
    try:
        return await track_vessel_with_fallback(name.strip())
    except VesselAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
