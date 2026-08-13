"""Workflow gates — enforce step-by-step importer pipeline with optional ADMIN override."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from infrastructure.activity.activity_service import log_activity
from models.database_models import Shipment
from modules.shipments import services as ship_svc
from modules.weboc.gd_service import _has_attachment, get_gd_or_404, stages_for
from modules.workflow.constants import (
    ACTION_GD_ADVANCE,
    ACTION_GD_SET_STATUS,
    ACTION_SET_DELIVERY,
    ACTION_UPLOAD_BL,
    ACTION_UPLOAD_EX_BOND_GD,
    ACTION_UPLOAD_FI,
    ACTION_UPLOAD_FINAL_GD,
    ACTION_UPLOAD_GD,
    ACTION_UPLOAD_GD_VIEW,
    ACTION_UPLOAD_INSURANCE,
    ACTION_UPLOAD_INTO_BOND_GD,
    ACTION_UPLOAD_INVOICE,
    ACTION_UPLOAD_ITEM_DETAILS,
    ACTION_UPLOAD_PACKING,
    MANAGER_ROLES,
)


def shipment_load_options():
    return [
        joinedload(Shipment.lc),
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.commercial_invoices),
        joinedload(Shipment.packing_lists),
        joinedload(Shipment.goods_declarations),
        joinedload(Shipment.financial_instruments),
        joinedload(Shipment.validations),
    ]


class WorkflowBlocked(HTTPException):
    def __init__(self, *, blocker: str, required_step: str, deadline: Optional[str] = None):
        super().__init__(
            status_code=409,
            detail={
                "code": "workflow_blocked",
                "blocker": blocker,
                "required_step": required_step,
                "deadline": deadline,
            },
        )


def _core_docs_done(s: Shipment) -> bool:
    return bool(s.bill_of_ladings and s.commercial_invoices and s.packing_lists)


def _validation_blocked(s: Shipment) -> tuple[bool, Optional[str]]:
    fails = [v for v in (s.validations or []) if (v.status or "").upper() == "FAIL"]
    if fails:
        return True, f"{len(fails)} validation check(s) failed"
    if (s.validation_status or "").upper() == "DISCREPANT":
        return True, "Document discrepancies must be resolved"
    return False, None


def _load_shipment(shipment_id: int, db: Session) -> Shipment:
    return ship_svc.get_shipment_or_404(shipment_id, db, options=shipment_load_options())


def get_workflow_state(shipment_id: int, db: Session) -> dict:
    from modules.shipments import journey_service as journey_svc

    journey = journey_svc.build_journey(shipment_id, db)
    timeline = journey_svc.build_timeline(shipment_id, db)
    steps = journey.get("steps") or []
    next_step = next((s for s in steps if s.get("status") not in ("done",)), None)
    return {
        "shipment_id": shipment_id,
        "completeness_pct": journey.get("completeness_pct"),
        "validation_blocked": journey.get("validation_blocked"),
        "clearance_type": journey.get("clearance_type"),
        "next_step": next_step,
        "steps": steps,
        "timeline": timeline,
    }


def log_override(
    db: Session,
    *,
    user_id: int,
    shipment_id: int,
    action: str,
    reason: str,
) -> None:
    log_activity(
        db,
        shipment_id,
        user_id,
        "WORKFLOW_OVERRIDE",
        detail=f"{action}: {reason[:400]}",
    )


def assert_step_allowed(
    db: Session,
    shipment_id: int,
    action: str,
    *,
    gd_id: Optional[int] = None,
    target_status: Optional[str] = None,
) -> None:
    """Raise WorkflowBlocked (409) unless prerequisites are met."""
    s = _load_shipment(shipment_id, db)
    has_bl = bool(s.bill_of_ladings)
    has_inv = bool(s.commercial_invoices)
    has_pkg = bool(s.packing_lists)
    core_done = _core_docs_done(s)
    val_blocked, val_msg = _validation_blocked(s)

    if action == ACTION_UPLOAD_BL:
        return

    if action == ACTION_UPLOAD_INVOICE:
        if not has_bl:
            raise WorkflowBlocked(blocker="Upload Bill of Lading first", required_step="docs_core")
        return

    if action == ACTION_UPLOAD_PACKING:
        if not has_bl:
            raise WorkflowBlocked(blocker="Upload Bill of Lading first", required_step="docs_core")
        if not has_inv:
            raise WorkflowBlocked(blocker="Upload Commercial Invoice first", required_step="docs_core")
        return

    if action in (ACTION_UPLOAD_FI, ACTION_UPLOAD_INSURANCE):
        if not core_done:
            raise WorkflowBlocked(
                blocker="Complete core documents (BL, Invoice, Packing) first",
                required_step="docs_core",
            )
        return

    if action in (
        ACTION_UPLOAD_GD,
        ACTION_UPLOAD_GD_VIEW,
        ACTION_UPLOAD_ITEM_DETAILS,
        ACTION_UPLOAD_FINAL_GD,
        ACTION_UPLOAD_INTO_BOND_GD,
        ACTION_UPLOAD_EX_BOND_GD,
    ):
        if not core_done:
            raise WorkflowBlocked(
                blocker="Complete core documents (BL, Invoice, Packing) first",
                required_step="docs_core",
            )
        if val_blocked:
            raise WorkflowBlocked(
                blocker=val_msg or "Resolve validation failures first",
                required_step="docs_validated",
            )
        gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
        if action == ACTION_UPLOAD_ITEM_DETAILS and gd:
            if not (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded or gd.gd_number):
                raise WorkflowBlocked(blocker="Upload GD View first", required_step="gd_started")
        if action == ACTION_UPLOAD_FINAL_GD and gd:
            if not _has_attachment(gd.gd_id, "ITEM_DETAILS", db):
                raise WorkflowBlocked(blocker="Upload Item Details first", required_step="gd_hc")
        if action == ACTION_UPLOAD_INTO_BOND_GD and gd:
            if not (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded):
                raise WorkflowBlocked(blocker="Upload GD View first", required_step="gd_started")
        if action == ACTION_UPLOAD_EX_BOND_GD and gd:
            if not (_has_attachment(gd.gd_id, "INTO_BOND_GD", db) or getattr(gd, "into_bond_gd_uploaded", False)):
                raise WorkflowBlocked(blocker="Upload Into-Bond GD first", required_step="gd_ib")
        return

    if action == ACTION_SET_DELIVERY:
        gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
        if gd:
            released = (gd.status or "") in ("RELEASED", "CLEARED", "INTO_BOND")
            if not released:
                raise WorkflowBlocked(
                    blocker="Customs clearance must be complete before marking delivered",
                    required_step="gd_hc" if (gd.gd_type or "") == "HOME_CONSUMPTION" else "gd_ib",
                )
        return

    if action in (ACTION_GD_ADVANCE, ACTION_GD_SET_STATUS) and gd_id:
        gd = get_gd_or_404(gd_id, db)
        if not core_done:
            raise WorkflowBlocked(
                blocker="Complete core documents before advancing customs",
                required_step="docs_core",
            )
        if val_blocked:
            raise WorkflowBlocked(
                blocker=val_msg or "Resolve validation failures first",
                required_step="docs_validated",
            )
        if action == ACTION_GD_ADVANCE:
            return
        if action == ACTION_GD_SET_STATUS and target_status:
            stages = stages_for(gd)
            cur = gd.status if gd.status in stages else "FILED"
            if target_status in stages and target_status != cur:
                try:
                    cur_idx = stages.index(cur)
                    tgt_idx = stages.index(target_status)
                    if tgt_idx > cur_idx + 1:
                        raise WorkflowBlocked(
                            blocker=f"Cannot skip from {cur} to {target_status} — advance one stage at a time",
                            required_step="gd_advance",
                        )
                except ValueError:
                    pass
        return

    # Unknown actions pass through
    return


def assert_step_allowed_for_user(
    db: Session,
    shipment_id: int,
    action: str,
    *,
    user_id: int,
    role_name: Optional[str],
    override_reason: Optional[str] = None,
    gd_id: Optional[int] = None,
    target_status: Optional[str] = None,
) -> None:
    if override_reason:
        if role_name not in MANAGER_ROLES:
            raise HTTPException(status_code=403, detail="Only ADMIN or MANAGER can override workflow gates")
        log_override(db, user_id=user_id, shipment_id=shipment_id, action=action, reason=override_reason)
        return
    assert_step_allowed(
        db,
        shipment_id,
        action,
        gd_id=gd_id,
        target_status=target_status,
    )
