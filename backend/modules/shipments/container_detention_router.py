"""Container detention API — report for container BLs only."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.shipments.container_detention_service import build_container_detention_report

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/container-detention", tags=["Container Detention"])


@router.get("/report")
def container_detention_report(
    state: str = Query(None),
    vessel: str = Query(None),
    port: str = Query(None),
    importer: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    q: str = Query(None),
    open_only: str = Query(None, description="If true, hide ended BLs"),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return build_container_detention_report(db, {
        "state": state,
        "vessel": vessel,
        "port": port,
        "importer": importer,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
        "open_only": open_only,
    })
