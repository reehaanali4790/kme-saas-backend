"""
LME Rates Matrix Endpoint
Provides bulletin-date-wise LME rates per region for comparison reporting.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
import logging

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.currency_rates import lme_rates_service as svc

router = APIRouter(prefix="/api/lme-rates", tags=["LME Rates"])
logger = logging.getLogger("uvicorn")


@router.get("/matrix")
def get_rates_matrix(
    group: str = Query("HR"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user)
):
    """
    Returns bulletin-date-wise LME rates matrix for China, Africa, Europe, UAE.
    Accepts a product group (HR, GP, CR, CRNGO, WR) instead of individual product codes.
    col1/col2 labels vary by group: Prime/Secondary for HR,GP,CR — LC/HC for WR — Secondary only for CRNGO.
    """
    return svc.build_rates_matrix(db, group, date_from, date_to)
