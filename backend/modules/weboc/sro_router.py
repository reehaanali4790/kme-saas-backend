"""
SRO / EDB Quota API.

An SRO entry grants a company a quota (approved_qty_mt) under a MAIN SRO number, with any
number of GROUP (child) SRO numbers beneath it, valid start_date..end_date.

Usage is computed live from GD data (modules/weboc/helpers/sro_usage.py):
  * manual quota lines added on a GD (SroQuotaItem) — always counted,
  * plus GD items extracted from the WeBOC Item Details document, auto-matched to a quota by
    company + SRO/HS reference. Low-confidence matches are reported, never counted.
Balance = approved_qty_mt - used.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from models.database_models import User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from modules.weboc import sro_service as svc
from modules.weboc.sro_schemas import ApprovalSave, SroQuotaItemSave

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/sro", tags=["SRO / EDB Quota"])

# Mutations require OPERATOR+ - VIEWER can read approvals/reports but not create/edit them.
_can_write = require_min_role("ADMIN", "MANAGER", "OPERATOR")


# ---------------------------------------------------------------------------
# EDB Approvals
# ---------------------------------------------------------------------------

@router.get("/approvals")
def list_approvals(db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(get_current_user)):
    usage = svc.compute_usage(db)
    return [svc.approval_to_dict(a, db, usage) for a in svc.list_approvals(db)]


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: int, db: Session = Depends(get_tenant_db),
                 current_user: User = Depends(get_current_user)):
    a = svc.get_approval_or_404(approval_id, db)
    usage = svc.compute_usage(db)
    out = svc.approval_to_dict(a, db, usage)
    out["items"] = [svc.item_to_dict(it) for it in svc.list_gd_items_for_approval(approval_id, db)]
    out["usage_lines"] = usage.get(approval_id, {}).get("lines", [])
    return out


@router.post("/approvals")
def create_approval(data: ApprovalSave, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    a = svc.create_approval(data, db, created_by=current_user.user_id)
    logger.info(f"SRO entry created: id={a.approval_id}, main_sro={a.main_sro_no}, "
                f"company={a.company_name}, qty={a.approved_qty_mt}, "
                f"groups={len(a.group_numbers)}")
    return svc.approval_to_dict(a, db)


@router.put("/approvals/{approval_id}")
def update_approval(approval_id: int, data: ApprovalSave, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    a = svc.update_approval(approval_id, data, db)
    return svc.approval_to_dict(a, db)


@router.delete("/approvals/{approval_id}")
def delete_approval(approval_id: int, db: Session = Depends(get_tenant_db),
                    current_user: User = Depends(_can_write)):
    svc.delete_approval(approval_id, db)
    return {"success": True, "approval_id": approval_id}


# ---------------------------------------------------------------------------
# SRO Quota Report
# ---------------------------------------------------------------------------

@router.get("/report/options")
def report_options(db: Session = Depends(get_tenant_db),
                   current_user: User = Depends(get_current_user)):
    return svc.report_options(db)


@router.get("/report")
def sro_quota_report(company: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                     main_sro_no: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                     hs_code: Optional[List[str]] = Query(None, description="OR'd when multiple"),
                     status: str = Query("All"),
                     db: Session = Depends(get_tenant_db),
                     current_user: User = Depends(get_current_user)):
    return svc.quota_report(db, company, main_sro_no, hs_code, status)


# ---------------------------------------------------------------------------
# Quota items (per GD)
# ---------------------------------------------------------------------------

@router.get("/gd/{gd_id}/items")
def list_gd_items(gd_id: int, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(get_current_user)):
    return [svc.item_to_dict(it) for it in svc.list_gd_items(gd_id, db)]


@router.post("/gd/{gd_id}/items")
def add_gd_item(gd_id: int, data: SroQuotaItemSave, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(_can_write)):
    return svc.item_to_dict(svc.add_gd_item(gd_id, data, db))


@router.put("/items/{item_id}")
def update_item(item_id: int, data: SroQuotaItemSave, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(_can_write)):
    return svc.item_to_dict(svc.update_item(item_id, data, db))


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_tenant_db),
                current_user: User = Depends(_can_write)):
    svc.delete_item(item_id, db)
    return {"success": True, "item_id": item_id}
