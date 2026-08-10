"""
LME Monitoring System - Currency Rates API
Manage USD and EUR exchange rates
Version: 1.0
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
import logging

from core.tenant import get_tenant_db
from core.exceptions import NotFoundError
from core.permissions import require_permission
from models.database_models import User
from modules.auth.dependencies import get_current_user
from integrations.nbp.nbp_rate_service import fetch_nbp_rates
from modules.currency_rates import service as svc
from modules.currency_rates.schemas import CurrencyRateCreate, CurrencyRateUpdate

router = APIRouter(prefix="/api/currency", tags=["Currency Rates"])
logger = logging.getLogger("uvicorn")

# Kept as a re-export: main.py's scheduled NBP job imports this name directly.
upsert_nbp_rate = svc.upsert_nbp_rate

_can_manage_rates = require_permission("upload_pdf")
_can_delete_rate = require_permission("manage_users")


@router.post("/rates/fetch-nbp")
def fetch_from_nbp(
    current_user: User = Depends(_can_manage_rates),
    db: Session = Depends(get_tenant_db),
):
    """Fetch the latest USD/EUR/AED TT-selling rates from the NBP ratesheet and save."""
    try:
        data = fetch_nbp_rates()
    except Exception as e:
        logger.error(f"NBP fetch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Could not fetch NBP rates: {str(e)}")
    result = svc.upsert_nbp_rate(db, data, current_user.user_id)
    return {"success": True, "message": f"NBP rates {result['action']} for {result['rate_date']}",
            "data": result}


@router.get("/rates/list")
def list_rates(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get list of currency rates"""
    try:
        return {"success": True, "data": svc.list_rates(db, limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rates/date/{rate_date}")
def get_rate_by_date(
    rate_date: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get currency rate for specific date"""
    try:
        rate = svc.get_rate_by_date(db, rate_date)
        if rate:
            return {"success": True, "found": True, "data": svc.rate_to_dict(rate)}
        return {"success": True, "found": False, "data": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rates/latest")
def get_latest_rate(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """Get latest currency rate"""
    try:
        rate = svc.get_latest_rate(db)
        if rate:
            return {"success": True, "data": svc.rate_to_dict(rate)}
        return {"success": True, "data": None, "message": "No rates found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rates/create")
def create_rate(
    rate_data: CurrencyRateCreate,
    current_user: User = Depends(_can_manage_rates),
    db: Session = Depends(get_tenant_db)
):
    """Create new currency rate"""
    try:
        new_rate = svc.create_rate(db, rate_data, current_user.user_id)
        return {
            "success": True,
            "message": "Currency rate created successfully",
            "data": {
                "rate_id": new_rate.rate_id,
                "rate_date": str(new_rate.rate_date),
                "usd_rate": float(new_rate.usd_rate),
                "eur_rate": float(new_rate.eur_rate)
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/rates/{rate_id}")
def update_rate(
    rate_id: int,
    rate_data: CurrencyRateUpdate,
    current_user: User = Depends(_can_manage_rates),
    db: Session = Depends(get_tenant_db)
):
    """Update existing currency rate"""
    try:
        svc.update_rate(db, rate_id, rate_data, current_user.user_id)
        return {"success": True, "message": "Currency rate updated successfully"}
    except NotFoundError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/rates/{rate_id}")
def delete_rate(
    rate_id: int,
    current_user: User = Depends(_can_delete_rate),
    db: Session = Depends(get_tenant_db)
):
    """Delete currency rate"""
    svc.delete_rate(db, rate_id, deleted_by=current_user.user_id)
    return {"success": True, "message": "Currency rate deleted successfully"}
