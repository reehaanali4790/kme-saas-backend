"""
Shared lookup API for the name masters (importers / suppliers / indentors / booked_by).
One parameterized endpoint backs the reusable searchable dropdown used across forms.

  GET  /api/lookup/{kind}?q=meen   -> [{id, name}, ...]  (search, max 50)
  POST /api/lookup/{kind}  {name}  -> {id, name, created}  (add-new / dedup)

kind in: importer | supplier | indentor | booked_by
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.reports import lookup_service as svc
from modules.reports.lookup_schemas import LookupCreate

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/lookup", tags=["Lookups"])


@router.get("/{kind}")
def search(kind: str, q: str = Query(None), db: Session = Depends(get_tenant_db),
           current_user: User = Depends(get_current_user)):
    return svc.search(db, kind, q)


@router.post("/{kind}")
def add(kind: str, data: LookupCreate, db: Session = Depends(get_tenant_db),
        current_user: User = Depends(get_current_user)):
    try:
        return svc.add(db, kind, data, current_user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
