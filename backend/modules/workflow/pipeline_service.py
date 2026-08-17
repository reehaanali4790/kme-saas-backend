"""Resolve whether a page/action is allowed and where to redirect if not."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models.database_models import Contract, LCMaster, Shipment
from modules.workflow.constants import (
    ACTION_CREATE_SHIPMENT,
    ACTION_UPLOAD_LC,
    DOC_TYPE_ACTIONS,
)
from modules.workflow import gates as gate_svc
from modules.workflow.gates import WorkflowBlocked
from modules.workflow.redirects import (
    contract_upload_href,
    contracts_list_href,
    create_lc_href,
    lc_detail_href,
    redirect_for_required_step,
    shipment_workflow_href,
)


def _blocked_result(
    *,
    blocker: str,
    required_step: str,
    shipment_id: Optional[int] = None,
    lc_id: Optional[int] = None,
    contract_id: Optional[int] = None,
    doc_type: Optional[str] = None,
) -> dict:
    href, label = redirect_for_required_step(
        required_step,
        shipment_id=shipment_id,
        lc_id=lc_id,
        contract_id=contract_id,
        doc_type=doc_type,
    )
    return {
        "allowed": False,
        "blocker": blocker,
        "required_step": required_step,
        "redirect_href": href,
        "redirect_label": label,
    }


def _allowed_result(**extra) -> dict:
    return {"allowed": True, "blocker": None, "required_step": None, "redirect_href": None, "redirect_label": None, **extra}


def _first_contract_awaiting_lc(db: Session) -> Optional[Contract]:
    return (
        db.query(Contract)
        .outerjoin(LCMaster, LCMaster.contract_id == Contract.contract_id)
        .filter(Contract.status != "CANCELLED", LCMaster.lc_id.is_(None))
        .order_by(Contract.created_at.desc())
        .first()
    )


def _org_has_lcs(db: Session) -> bool:
    return db.query(LCMaster.lc_id).limit(1).first() is not None


def _org_has_contracts(db: Session) -> bool:
    return db.query(Contract.contract_id).filter(Contract.status != "CANCELLED").limit(1).first() is not None


def resolve_next_step(
    db: Session,
    *,
    context: str = "page_load",
    page: Optional[str] = None,
    action: Optional[str] = None,
    contract_id: Optional[int] = None,
    lc_id: Optional[int] = None,
    shipment_id: Optional[int] = None,
    doc_type: Optional[str] = None,
) -> dict:
    """Context-aware guard for frontend page/action interceptors."""
    page = (page or "").lower()
    action = (action or "").lower() or None
    doc_type = (doc_type or "").lower() or None

    # --- Shipments: create shipment (LC-backed or non-LC from contract) ---
    if page == "shipments" and action in ("create_shipment", None):
        if action == "create_shipment" or (context == "action"):
            if not _org_has_contracts(db):
                return _blocked_result(
                    blocker="Upload a supplier contract before creating shipments",
                    required_step="contract",
                )
            # Non-LC path: contract alone is enough; LC path still preferred when LCs exist.
            if not _org_has_lcs(db):
                return _allowed_result(hint="non_lc_available")

    # --- Create LC without contract ---
    if page == "create-lc":
        if contract_id:
            contract = db.query(Contract).filter(Contract.contract_id == contract_id).first()
            if not contract:
                return _blocked_result(
                    blocker="Contract not found — pick a valid contract",
                    required_step="contract_pick",
                )
            lc = db.query(LCMaster).filter(LCMaster.contract_id == contract_id).first()
            if lc and action in ("upload_lc", None):
                return _blocked_result(
                    blocker=f"LC {lc.lc_number or lc.lc_id} already opened for this contract",
                    required_step="lc_exists",
                    lc_id=lc.lc_id,
                    contract_id=contract_id,
                )
        elif action in ("upload_lc", None) and context in ("page_load", "action"):
            if not _org_has_contracts(db):
                return _blocked_result(
                    blocker="Upload a supplier contract before opening an LC",
                    required_step="contract",
                )
            awaiting = _first_contract_awaiting_lc(db)
            if awaiting:
                return _blocked_result(
                    blocker="Select a contract before uploading the LC — every LC must match a contract",
                    required_step="lc",
                    contract_id=awaiting.contract_id,
                )
            return _blocked_result(
                blocker="Link this LC to a contract from the Contracts page first",
                required_step="contract_pick",
            )

    # --- LC table: upload LC when no contracts ---
    if page == "lc-table" and action == "upload_lc":
        if not _org_has_contracts(db):
            return _blocked_result(
                blocker="Upload a supplier contract before creating an LC",
                required_step="contract",
            )
        awaiting = _first_contract_awaiting_lc(db)
        if awaiting:
            return _blocked_result(
                blocker="Open LC from your contract — select the contract first",
                required_step="lc",
                contract_id=awaiting.contract_id,
            )

    # --- Shipment doc upload: delegate to gates ---
    if page == "shipment-doc-upload" and shipment_id and doc_type:
        gate_action = DOC_TYPE_ACTIONS.get(doc_type)
        if gate_action:
            try:
                gate_svc.assert_step_allowed(db, shipment_id, gate_action)
            except WorkflowBlocked as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                req = detail.get("required_step", "docs_core")
                blocker = detail.get("blocker", "Complete the prior step first")
                next_doc = "bl"
                if "invoice" in gate_action:
                    next_doc = "bl" if "BL" in blocker else "invoice"
                elif "packing" in gate_action:
                    next_doc = "invoice" if "Invoice" in blocker else "packing"
                elif "gd" in doc_type.lower():
                    next_doc = doc_type
                return _blocked_result(
                    blocker=blocker,
                    required_step=req,
                    shipment_id=shipment_id,
                    lc_id=lc_id,
                    doc_type=next_doc if req == "docs_core" else doc_type,
                )

    # --- Generic action checks (API-style) ---
    if action == ACTION_UPLOAD_LC and not contract_id:
        if not _org_has_contracts(db):
            return _blocked_result(blocker="Upload a contract first", required_step="contract")
        return _blocked_result(blocker="Link LC to a contract", required_step="contract_pick")

    if action == ACTION_CREATE_SHIPMENT:
        if not _org_has_contracts(db):
            return _blocked_result(blocker="Upload a contract first", required_step="contract")
        if lc_id:
            lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
            if not lc:
                return _blocked_result(blocker="LC not found", required_step="lc")
        # non-LC create: contract_id validated at API layer

    # --- Empty-state hints ---
    if page == "shipments" and context == "empty_state":
        if not _org_has_contracts(db):
            return _blocked_result(
                blocker="Start by uploading your supplier contract",
                required_step="contract",
            )
        if not _org_has_lcs(db):
            return _allowed_result(hint="Create a non-LC shipment from your contract")
        awaiting = _first_contract_awaiting_lc(db)
        if awaiting:
            return _blocked_result(
                blocker="Open an LC for your contract, then create a shipment — or use non-LC for TT/CAD",
                required_step="lc",
                contract_id=awaiting.contract_id,
            )
        return _blocked_result(
            blocker="Create an LC from Contracts, or start a non-LC shipment",
            required_step="contract_pick",
        )

    if page == "contracts" and context == "empty_state":
        return _blocked_result(
            blocker="Upload your first supplier contract to begin the import pipeline",
            required_step="contract",
        )

    if page == "lc-table" and context == "empty_state":
        if not _org_has_contracts(db):
            return _blocked_result(
                blocker="Upload a contract first — every LC must match a purchase contract",
                required_step="contract",
            )
        awaiting = _first_contract_awaiting_lc(db)
        if awaiting:
            return _blocked_result(
                blocker="Open an LC for a contract awaiting bank issuance",
                required_step="lc",
                contract_id=awaiting.contract_id,
            )

    return _allowed_result()


def enrich_blocked_detail(
    detail: dict,
    *,
    shipment_id: Optional[int] = None,
    lc_id: Optional[int] = None,
    contract_id: Optional[int] = None,
    doc_type: Optional[str] = None,
) -> dict:
    """Add redirect_href/redirect_label to a workflow_blocked 409 detail dict."""
    if detail.get("code") != "workflow_blocked":
        return detail
    req = detail.get("required_step") or "docs_core"
    href, label = redirect_for_required_step(
        req,
        shipment_id=shipment_id,
        lc_id=lc_id,
        contract_id=contract_id,
        doc_type=doc_type,
    )
    return {**detail, "redirect_href": href, "redirect_label": label}


def gate_exception_to_detail(
    exc: WorkflowBlocked,
    *,
    shipment_id: Optional[int] = None,
    lc_id: Optional[int] = None,
    contract_id: Optional[int] = None,
    doc_type: Optional[str] = None,
) -> dict:
    detail = exc.detail if isinstance(exc.detail, dict) else {"blocker": str(exc.detail)}
    return enrich_blocked_detail(
        detail,
        shipment_id=shipment_id,
        lc_id=lc_id,
        contract_id=contract_id,
        doc_type=doc_type,
    )
