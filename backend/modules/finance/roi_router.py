"""
ROI Intelligence API — financial exposure, forecasting, and LME opportunity views.

Ported from rehan/newchanges (backend/api/roi_endpoints.py).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.schemas import PaginationParams
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.finance.roi_service import (
    RoiFilters,
    bank_exposure_summary,
    gd_payables_forecast,
    lc_payables_forecast,
    lme_opportunities,
    penalty_and_risk_exposure,
    roi_filter_options,
    roi_overview,
)

logger = logging.getLogger("uvicorn")
router = APIRouter(prefix="/api/roi", tags=["ROI Intelligence"])


def _parse_filters(
    importer: Optional[List[str]] = None,
    buyer: Optional[List[str]] = None,
    vessel: Optional[List[str]] = None,
    bank: Optional[List[str]] = None,
) -> RoiFilters:
    def parse_list(lst: Optional[List[str]]) -> Optional[List[str]]:
        if not lst:
            return None
        res = []
        for x in lst:
            if x:
                for part in str(x).split(","):
                    p = part.strip()
                    if p:
                        res.append(p)
        return res or None

    return RoiFilters(
        importer=parse_list(importer),
        buyer=parse_list(buyer),
        vessel=parse_list(vessel),
        bank=parse_list(bank),
    )


@router.get("/options")
def get_filter_options(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return roi_filter_options(db)


@router.get("/overview")
def get_overview(
    importer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    filters = _parse_filters(importer, buyer, vessel, bank)
    try:
        result = roi_overview(db, filters=filters)
        if result.get("errors"):
            logger.warning("roi overview partial errors: %s", result["errors"])
        return result
    except Exception as exc:
        logger.exception("roi overview failed completely")
        raise HTTPException(status_code=500, detail=f"ROI overview failed: {type(exc).__name__}") from exc


@router.get("/lc-payables")
def get_lc_payables(
    horizon_days: int = Query(90, ge=30, le=365),
    importer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    params: PaginationParams = Depends(),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return lc_payables_forecast(
        db, horizon_days=horizon_days, filters=_parse_filters(importer, buyer, vessel, bank),
        page=params.page, page_size=params.page_size)


@router.get("/bank-exposure")
def get_bank_exposure(
    importer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return bank_exposure_summary(db, filters=_parse_filters(importer, buyer, vessel, bank))


@router.get("/gd-payables")
def get_gd_payables(
    importer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    params: PaginationParams = Depends(),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return gd_payables_forecast(
        db, filters=_parse_filters(importer, buyer, vessel, bank),
        page=params.page, page_size=params.page_size)


@router.get("/lme-opportunities")
def get_lme_opportunities(
    favorable_only: bool = Query(False),
    importer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    buyer: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    vessel: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    bank: Optional[List[str]] = Query(None, description="OR'd when multiple"),
    params: PaginationParams = Depends(),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return lme_opportunities(
        db, favorable_only=favorable_only,
        filters=_parse_filters(importer, buyer, vessel, bank),
        page=params.page, page_size=params.page_size)


@router.get("/risk-exposure")
def get_risk_exposure(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return penalty_and_risk_exposure(db)
