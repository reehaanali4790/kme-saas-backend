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
    ACTION_CREATE_SHIPMENT,
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
    ACTION_UPLOAD_LC,
    ACTION_UPLOAD_PACKING,
    MANAGER_ROLES,
)
from modules.workflow.import_paths import (
    DOCS_RECEPTION_AWAITING,
    fi_required,
    missing_required_docs,
    normalize_import_mode,
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
    def __init__(
        self,
        *,
        blocker: str,
        required_step: str,
        deadline: Optional[str] = None,
        shipment_id: Optional[int] = None,
        lc_id: Optional[int] = None,
        contract_id: Optional[int] = None,
        doc_type: Optional[str] = None,
    ):
        from modules.workflow.pipeline_service import enrich_blocked_detail

        detail = enrich_blocked_detail(
            {
                "code": "workflow_blocked",
                "blocker": blocker,
                "required_step": required_step,
                "deadline": deadline,
            },
            shipment_id=shipment_id,
            lc_id=lc_id,
            contract_id=contract_id,
            doc_type=doc_type,
        )
        super().__init__(status_code=409, detail=detail)


def _core_docs_done(s: Shipment) -> bool:
    return not missing_required_docs(
        normalize_import_mode(s.import_mode),
        has_bl=bool(s.bill_of_ladings),
        has_invoice=bool(s.commercial_invoices),
        has_packing=bool(s.packing_lists),
        has_fi=bool(s.financial_instruments),
    )


def _assert_gd_docs_reception(s: Shipment, shipment_id: int) -> None:
    """Block GD when vessel is on port and required docs are still missing."""
    from modules.shipments.docs_reception import docs_reception_summary

    summary = docs_reception_summary(s)
    if (s.docs_reception_status or "") == DOCS_RECEPTION_AWAITING or summary.get("on_port"):
        missing = summary.get("missing_required_docs") or []
        if missing and summary.get("on_port"):
            raise WorkflowBlocked(
                blocker=f"Vessel on port — missing required documents: {', '.join(missing)}",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
            )


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
            raise WorkflowBlocked(
                blocker="Upload Bill of Lading first",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
            )
        return

    if action == ACTION_UPLOAD_PACKING:
        if not has_bl:
            raise WorkflowBlocked(
                blocker="Upload Bill of Lading first",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
            )
        if not has_inv:
            raise WorkflowBlocked(
                blocker="Upload Commercial Invoice first",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="invoice",
            )
        return

    if action in (ACTION_UPLOAD_FI, ACTION_UPLOAD_INSURANCE):
        if action == ACTION_UPLOAD_FI and not fi_required(s.import_mode):
            return
        if not _core_docs_done(s):
            raise WorkflowBlocked(
                blocker="Complete core documents (BL, Invoice, Packing) first",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
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
        if not _core_docs_done(s):
            raise WorkflowBlocked(
                blocker="Complete core documents (BL, Invoice, Packing) first",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
            )
        _assert_gd_docs_reception(s, shipment_id)
        if val_blocked:
            raise WorkflowBlocked(
                blocker=val_msg or "Resolve validation failures first",
                required_step="docs_validated",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
            )
        gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
        if action == ACTION_UPLOAD_ITEM_DETAILS and gd:
            if not (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded or gd.gd_number):
                raise WorkflowBlocked(
                    blocker="Upload GD View first",
                    required_step="gd_started",
                    shipment_id=shipment_id,
                    lc_id=s.lc_id,
                    doc_type="gdview",
                )
        if action == ACTION_UPLOAD_FINAL_GD and gd:
            if not _has_attachment(gd.gd_id, "ITEM_DETAILS", db):
                raise WorkflowBlocked(
                    blocker="Upload Item Details first",
                    required_step="gd_hc",
                    shipment_id=shipment_id,
                    lc_id=s.lc_id,
                    doc_type="itemdetails",
                )
        if action == ACTION_UPLOAD_INTO_BOND_GD and gd:
            if not (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded):
                raise WorkflowBlocked(
                    blocker="Upload GD View first",
                    required_step="gd_started",
                    shipment_id=shipment_id,
                    lc_id=s.lc_id,
                    doc_type="gdview",
                )
        if action == ACTION_UPLOAD_EX_BOND_GD and gd:
            if not (_has_attachment(gd.gd_id, "INTO_BOND_GD", db) or getattr(gd, "into_bond_gd_uploaded", False)):
                raise WorkflowBlocked(
                    blocker="Upload Into-Bond GD first",
                    required_step="gd_ib",
                    shipment_id=shipment_id,
                    lc_id=s.lc_id,
                    doc_type="intobondgd",
                )
        return

    if action == ACTION_SET_DELIVERY:
        gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
        if gd:
            released = (gd.status or "") in ("RELEASED", "CLEARED", "INTO_BOND")
            if not released:
                raise WorkflowBlocked(
                    blocker="Customs clearance must be complete before marking delivered",
                    required_step="gd_hc" if (gd.gd_type or "") == "HOME_CONSUMPTION" else "gd_ib",
                    shipment_id=shipment_id,
                    lc_id=s.lc_id,
                )
        return

    if action in (ACTION_GD_ADVANCE, ACTION_GD_SET_STATUS) and gd_id:
        gd = get_gd_or_404(gd_id, db)
        if not core_done:
            raise WorkflowBlocked(
                blocker="Complete core documents before advancing customs",
                required_step="docs_core",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
                doc_type="bl",
            )
        if val_blocked:
            raise WorkflowBlocked(
                blocker=val_msg or "Resolve validation failures first",
                required_step="docs_validated",
                shipment_id=shipment_id,
                lc_id=s.lc_id,
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
                            shipment_id=shipment_id,
                            lc_id=s.lc_id,
                        )
                except ValueError:
                    pass
        return

    # Unknown actions pass through
    return


def assert_lc_upload_allowed(
    db: Session,
    *,
    contract_id: Optional[int] = None,
) -> None:
    """Contract must exist and be linked before LC upload/create."""
    from models.database_models import Contract, LCMaster

    if not contract_id:
        raise WorkflowBlocked(
            blocker="Link this LC to a supplier contract first",
            required_step="contract_pick",
        )
    contract = db.query(Contract).filter(Contract.contract_id == contract_id).first()
    if not contract:
        raise WorkflowBlocked(
            blocker="Contract not found — select a valid contract",
            required_step="contract_pick",
        )
    existing = db.query(LCMaster).filter(LCMaster.contract_id == contract_id).first()
    if existing:
        raise WorkflowBlocked(
            blocker=f"LC already opened for this contract ({existing.lc_number or existing.lc_id})",
            required_step="lc_exists",
            lc_id=existing.lc_id,
            contract_id=contract_id,
        )


def assert_shipment_create_allowed(
    db: Session,
    *,
    contract_id: int,
    import_mode: str = "LC_BACKED",
    lc_id: Optional[int] = None,
) -> None:
    """Contract must exist; LC required only for LC-backed imports."""
    from models.database_models import Contract, LCMaster

    contract = db.query(Contract).filter(Contract.contract_id == contract_id).first()
    if not contract:
        raise WorkflowBlocked(
            blocker="Contract not found — upload or select a contract first",
            required_step="contract",
            contract_id=contract_id,
        )
    mode = normalize_import_mode(import_mode)
    if mode == "LC_BACKED":
        if not lc_id:
            raise WorkflowBlocked(
                blocker="LC is required for LC-backed shipments",
                required_step="lc",
                contract_id=contract_id,
            )
        lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
        if not lc:
            raise WorkflowBlocked(
                blocker="LC not found — create or import an LC first",
                required_step="lc",
                lc_id=lc_id,
                contract_id=contract_id,
            )


def assert_lc_upload_allowed_for_user(
    db: Session,
    *,
    contract_id: Optional[int],
    user_id: int,
    role_name: Optional[str],
    override_reason: Optional[str] = None,
) -> None:
    if override_reason:
        if role_name not in MANAGER_ROLES:
            raise HTTPException(status_code=403, detail="Only ADMIN or MANAGER can override workflow gates")
        return
    assert_lc_upload_allowed(db, contract_id=contract_id)


def assert_shipment_create_allowed_for_user(
    db: Session,
    *,
    contract_id: int,
    import_mode: str = "LC_BACKED",
    lc_id: Optional[int] = None,
    user_id: int,
    role_name: Optional[str],
    override_reason: Optional[str] = None,
) -> None:
    if override_reason:
        if role_name not in MANAGER_ROLES:
            raise HTTPException(status_code=403, detail="Only ADMIN or MANAGER can override workflow gates")
        return
    assert_shipment_create_allowed(
        db, contract_id=contract_id, import_mode=import_mode, lc_id=lc_id,
    )


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
