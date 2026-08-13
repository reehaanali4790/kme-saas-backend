"""Workflow API — gates state and unified My Work feed."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.workflow import gates as gate_svc
from modules.workflow import my_work_service as my_work_svc
from modules.workflow import pipeline_service as pipeline_svc

router = APIRouter(prefix="/api/workflow", tags=["Workflow"])


@router.get("/resolve-next-step")
def resolve_next_step(
    context: str = Query("page_load", description="page_load | action | empty_state"),
    page: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    contract_id: Optional[int] = Query(None),
    lc_id: Optional[int] = Query(None),
    shipment_id: Optional[int] = Query(None),
    doc_type: Optional[str] = Query(None),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return pipeline_svc.resolve_next_step(
        db,
        context=context,
        page=page,
        action=action,
        contract_id=contract_id,
        lc_id=lc_id,
        shipment_id=shipment_id,
        doc_type=doc_type,
    )


@router.get("/my-work")
def get_my_work(
    scope: str = Query("today", description="today | week | all"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return my_work_svc.list_my_work(db, scope=scope, limit=limit)


@router.get("/shipments/{shipment_id}/state")
def get_shipment_workflow_state(
    shipment_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return gate_svc.get_workflow_state(shipment_id, db)
