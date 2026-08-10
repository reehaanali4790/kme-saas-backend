"""Authorization dependencies for multi-tenant RBAC."""

from typing import Callable

from fastapi import Depends, Request

from core.exceptions import ForbiddenError
from core.tenant import get_tenant_db
from models.platform_models import User
from modules.auth.dependencies import decode_access_token_from_request, get_current_user
from modules.auth.services import AuthService
from sqlalchemy.orm import Session


def _role_from_request(request: Request) -> str | None:
    payload = decode_access_token_from_request(request)
    if payload:
        return payload.get("role")
    return None


def require_permission(permission: str) -> Callable[..., User]:
    def _check(
        request: Request,
        current_user: User = Depends(get_current_user),
        tenant_db: Session = Depends(get_tenant_db),
    ) -> User:
        role_name = _role_from_request(request)
        if not role_name or not AuthService.check_permission_for_role(tenant_db, role_name, permission):
            raise ForbiddenError(f'This action requires the "{permission}" permission.')
        return current_user

    return _check


def require_min_role(*allowed_role_names: str) -> Callable[..., User]:
    def _check(
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> User:
        role_name = _role_from_request(request)
        if role_name not in allowed_role_names:
            raise ForbiddenError(
                f"This action requires one of these roles: {', '.join(allowed_role_names)}."
            )
        return current_user

    return _check


def require_platform_admin() -> Callable[..., User]:
    def _check(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.is_platform_admin:
            raise ForbiddenError("Platform administrator access required.")
        return current_user

    return _check
