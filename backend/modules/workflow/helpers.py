"""Workflow gate helpers."""
from __future__ import annotations

from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from core.permissions import _role_from_request


def role_from_request(request: Request) -> str:
    return _role_from_request(request) or "VIEWER"


def check_gate(
    db: Session,
    request: Request,
    shipment_id: int,
    action: str,
    *,
    user_id: int,
    override_reason: Optional[str] = None,
    gd_id: Optional[int] = None,
    target_status: Optional[str] = None,
) -> None:
    from modules.workflow import gates as gate_svc

    gate_svc.assert_step_allowed_for_user(
        db,
        shipment_id,
        action,
        user_id=user_id,
        role_name=role_from_request(request),
        override_reason=override_reason,
        gd_id=gd_id,
        target_status=target_status,
    )


def check_lc_upload(
    db: Session,
    request: Request,
    *,
    contract_id: Optional[int],
    user_id: int,
    override_reason: Optional[str] = None,
) -> None:
    from modules.workflow import gates as gate_svc

    gate_svc.assert_lc_upload_allowed_for_user(
        db,
        contract_id=contract_id,
        user_id=user_id,
        role_name=role_from_request(request),
        override_reason=override_reason,
    )


def check_shipment_create(
    db: Session,
    request: Request,
    *,
    contract_id: int,
    import_mode: str = "LC_BACKED",
    lc_id: Optional[int] = None,
    user_id: int,
    override_reason: Optional[str] = None,
) -> None:
    from modules.workflow import gates as gate_svc

    gate_svc.assert_shipment_create_allowed_for_user(
        db,
        contract_id=contract_id,
        import_mode=import_mode,
        lc_id=lc_id,
        user_id=user_id,
        role_name=role_from_request(request),
        override_reason=override_reason,
    )
