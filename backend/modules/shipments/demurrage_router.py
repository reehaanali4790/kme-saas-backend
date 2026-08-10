"""
Demurrage endpoints — global defaults config + at-risk BL listing.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from modules.shipments import demurrage_service as svc
from modules.shipments.demurrage_schemas import DemurrageAmountUpdate, DemurrageConfigUpdate
from utils.parsing import parse_date

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/demurrage", tags=["Demurrage"])

# Config changes require OPERATOR+ - VIEWER can read config/at-risk lists but not change them.
_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


# ---------------------------------------------------------------------------
# GET /api/demurrage/config
# ---------------------------------------------------------------------------
@router.get("/config")
def get_config(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return svc.config_to_dict(svc.get_or_create_config(db))


# ---------------------------------------------------------------------------
# PUT /api/demurrage/config
# ---------------------------------------------------------------------------
@router.put("/config")
def update_config(
    data: DemurrageConfigUpdate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    cfg = svc.update_config(data, db)
    logger.info(f"Demurrage config updated by {current_user.username}")
    return {"success": True, "config": svc.config_to_dict(cfg)}


# ---------------------------------------------------------------------------
# GET /api/demurrage/at-risk  — BLs accruing or about to
# ---------------------------------------------------------------------------
@router.get("/at-risk")
def at_risk(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return svc.at_risk_bls(db)


# ---------------------------------------------------------------------------
# GET /api/demurrage/report — filterable, one-row-per-BL demurrage report
# ---------------------------------------------------------------------------
@router.get("/report")
def report(
    state: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    vessel: Optional[str] = Query(None),
    port: Optional[str] = Query(None),
    importer: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    open_only: Optional[str] = Query(None),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return svc.demurrage_report(
        db,
        state=state,
        date_from=parse_date(date_from),
        date_to=parse_date(date_to),
        vessel=vessel,
        port=port,
        importer=importer,
        q=q,
        open_only=open_only == "1",
    )


# ---------------------------------------------------------------------------
# PATCH /api/demurrage/bl/{bl_id}/amount — record actual demurrage paid (delivered only)
# ---------------------------------------------------------------------------
@router.patch("/bl/{bl_id}/amount")
def update_bl_amount(
    bl_id: int,
    data: DemurrageAmountUpdate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_can_write),
):
    try:
        result = svc.update_bl_demurrage_amount(
            db,
            bl_id,
            data.demurrage_total_amount,
            data.demurrage_currency,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(f"Demurrage amount updated on BL {bl_id} by {current_user.username}")
    return result
