"""
LME Calculation Endpoints
API routes for LME calculation page
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.exceptions import NotFoundError
from modules.auth.dependencies import get_current_user
from models.database_models import User
from modules.currency_rates import lme_calculation_service as svc
from modules.currency_rates.lme_calculation_schemas import (
    LCForCalculation, FormulaTestRequest, FormulaTestResponse, ApplyRatesRequest,
)

router = APIRouter(prefix="/api/calculate", tags=["LME Calculation"])


@router.get("/lcs-for-calculation", response_model=List[LCForCalculation])
def get_lcs_for_calculation(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get list of all LCs with their calculated LME values for the calculation page"""
    try:
        return svc.list_lcs_for_calculation(db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching LCs: {str(e)}")


@router.post("/calculate/{lc_id}")
def calculate_lc(
    lc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Calculate LME for a specific LC"""
    try:
        return svc.calculate_lc(db, lc_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error calculating LME: {str(e)}")


@router.post("/test-match", response_model=FormulaTestResponse)
def test_formula_match(
    request: FormulaTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Test which formula matches for given product/origin/quality"""
    try:
        return svc.test_formula_match(
            db, request.product_code, request.origin, request.quality,
            request.grade, request.product_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error testing formula: {str(e)}")


@router.post("/calculate-all")
def calculate_all_lcs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Calculate LME for all active LCs"""
    try:
        return svc.calculate_all_lcs(db)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error calculating LMEs: {str(e)}")


@router.get("/bulletin-impact/{bulletin_id}")
def get_bulletin_impact(
    bulletin_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """
    For a given bulletin, calculate new LME for all LCs in the last 40-day window
    and compare against their stored baseline_lme and current_lme.
    """
    try:
        return svc.bulletin_impact(db, bulletin_id)
    except NotFoundError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-rates")
def apply_rates(
    req: ApplyRatesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """
    Calculate LME using a specific bulletin and save results to current_lme.
    If lc_ids is empty, applies to all LCs in the 40-day window.
    """
    try:
        return svc.apply_rates(db, req.bulletin_id, req.lc_ids)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
