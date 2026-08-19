"""
Report Master API — user-defined report templates and ad-hoc queries.

Ported from rehan/newchanges (backend/api/report_master_endpoints.py).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.plan_limits import require_plan_feature
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.reports.report_field_catalog import get_catalog, GRAINS, MASTER_GRAIN
from modules.reports.report_master_service import (
    list_templates,
    get_template,
    create_template,
    update_template,
    delete_template,
    execute_query,
    template_to_dict,
)

router = APIRouter(
    prefix="/api/report-master",
    tags=["Report Master"],
    dependencies=[Depends(require_plan_feature("report_builder"))],
)
router_underscore = APIRouter(
    prefix="/api/report_master",
    tags=["Report Master (Underscore Alias)"],
    dependencies=[Depends(require_plan_feature("report_builder"))],
)


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    grain: str
    columns: list[str] = Field(..., min_length=1)
    filters: dict[str, Any] = Field(default_factory=dict)
    sort_field: Optional[str] = None
    sort_dir: Optional[str] = "asc"


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    columns: Optional[list[str]] = None
    filters: Optional[dict[str, Any]] = None
    sort_field: Optional[str] = None
    sort_dir: Optional[str] = None
    sidebar_order: Optional[int] = None


class QueryRequest(BaseModel):
    grain: Optional[str] = None
    columns: Optional[list[str]] = None
    filters: dict[str, Any] = Field(default_factory=dict)
    sort_field: Optional[str] = None
    sort_dir: Optional[str] = "asc"
    template_id: Optional[int] = None


@router.get("/catalog")
@router_underscore.get("/catalog")
def catalog(grain: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Column and filter definitions for the Report Master builder."""
    try:
        return get_catalog(grain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/templates")
@router_underscore.get("/templates")
def templates_list(db: Session = Depends(get_tenant_db), current_user: User = Depends(get_current_user)):
    """All saved templates for the current user (shown in sidebar)."""
    try:
        items = list_templates(db, current_user.user_id)
        return {"count": len(items), "items": items}
    except Exception:
        return {"count": 0, "items": []}


@router.post("/templates", status_code=201)
def templates_create(
    body: TemplateCreate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    try:
        t = create_template(db, current_user.user_id, body.model_dump())
        return {"success": True, "template": template_to_dict(t)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/templates/{template_id}")
def templates_get(
    template_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    t = get_template(db, template_id, current_user.user_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return template_to_dict(t)


@router.put("/templates/{template_id}")
def templates_update(
    template_id: int,
    body: TemplateUpdate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    t = get_template(db, template_id, current_user.user_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        updated = update_template(db, t, body.model_dump(exclude_unset=True))
        return {"success": True, "template": template_to_dict(updated)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/templates/{template_id}")
def templates_delete(
    template_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    t = get_template(db, template_id, current_user.user_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    delete_template(db, t)
    return {"success": True}


@router.post("/query")
def run_query(
    body: QueryRequest,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    """Run a report — from a saved template and/or ad-hoc column/filter selection."""
    grain = body.grain
    columns = body.columns
    filters = dict(body.filters or {})
    sort_field = body.sort_field
    sort_dir = body.sort_dir or "asc"
    template_id = body.template_id

    if template_id:
        t = get_template(db, template_id, current_user.user_id)
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
        grain = t.grain
        columns = t.columns
        merged = dict(t.filters or {})
        merged.update(filters)
        filters = merged
        sort_field = sort_field or t.sort_field
        sort_dir = sort_dir or t.sort_dir or "asc"

    if not grain:
        grain = MASTER_GRAIN
    if grain.lower() not in GRAINS:
        raise HTTPException(status_code=400, detail=f"grain must be one of: {', '.join(GRAINS)}")
    if not columns:
        raise HTTPException(status_code=400, detail="columns are required")

    try:
        result = execute_query(
            db,
            grain=grain,
            columns=columns,
            filters=filters,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
        if template_id:
            result["template_id"] = template_id
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
