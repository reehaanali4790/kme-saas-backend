"""
Auth dependencies shared across routers (avoids circular imports with tenant module).
"""

from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from config.database import get_platform_db, set_platform_search_path
from models.platform_models import User, UserSession, OrganizationMembership
from modules.auth.services import AuthService


def decode_access_token_from_request(request: Request) -> Optional[dict]:
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get("access_token")
    if not token:
        return None
    payload = AuthService.decode_token(token)
    if payload and payload.get("type") == "access":
        return payload
    return None


def _get_token_from_request(request: Request) -> tuple[Optional[str], bool]:
    token = None
    using_cookie = False
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    else:
        token = request.cookies.get("access_token")
        if token:
            using_cookie = True
    return token, using_cookie


async def get_current_user(
    request: Request,
    db: Session = Depends(get_platform_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token, using_cookie = _get_token_from_request(request)
    if not token:
        raise credentials_exception

    if using_cookie and request.method in ("POST", "PUT", "DELETE", "PATCH"):
        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token") or request.headers.get("x-xsrf-token")
        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token validation failed")

    payload = AuthService.decode_token(token)
    if payload is None:
        raise credentials_exception

    token_type = payload.get("type")
    if token_type not in ("access", "pre_auth"):
        raise credentials_exception

    user_id_val = payload.get("sub")
    if user_id_val is None:
        raise credentials_exception
    user_id = int(user_id_val)

    if token_type == "access":
        from core.redis import redis_cache

        hashed_token = AuthService.hash_token(token)
        org_id = payload.get("org_id") or "none"
        cache_key = f"lme:session_active:{org_id}:{hashed_token}"

        is_active = redis_cache.get(cache_key)
        if is_active is None:
            session = db.query(UserSession).filter(
                UserSession.session_token == hashed_token,
                UserSession.active == True,
            ).first()
            if session is None:
                raise credentials_exception
            try:
                redis_cache.set(cache_key, "1", ex=1800)
            except Exception:
                pass
        elif is_active != "1":
            raise credentials_exception

    user = AuthService.get_user_by_id(db, user_id=user_id)
    if user is None or not user.active:
        raise credentials_exception

    return user


def get_user_role_for_tenant(db: Session, user: User, org_id: int) -> Optional[str]:
    set_platform_search_path(db)
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user.user_id,
        OrganizationMembership.organization_id == org_id,
    ).first()
    return membership.role_name if membership else None


def resolve_user_permissions(db: Session, role_name: str) -> dict:
    """Load permission flags from tenant schema roles table."""
    from models.database_models import Role

    role = db.query(Role).filter(Role.role_name == role_name).first()
    if not role:
        return {}
    return {
        "can_import_lc": role.can_import_lc,
        "can_upload_pdf": role.can_upload_pdf,
        "can_edit_lc": role.can_edit_lc,
        "can_delete_lc": role.can_delete_lc,
        "can_manage_users": role.can_manage_users,
        "can_reopen_lc": role.can_reopen_lc,
        "can_change_lc_status": role.can_change_lc_status,
        "can_view_dashboard": role.can_view_dashboard,
        "can_configure_alerts": role.can_configure_alerts,
        "can_view_all_lcs": role.can_view_all_lcs,
        "can_export_reports": role.can_export_reports,
    }


def build_token_payload(
    user: User,
    org_id: int,
    tenant_schema: str,
    role_name: str,
    plan_slug: Optional[str] = None,
) -> dict:
    data = {
        "sub": user.user_id,
        "username": user.username,
        "org_id": org_id,
        "tenant_schema": tenant_schema,
        "role": role_name,
        "type": "access",
    }
    if plan_slug:
        data["plan_slug"] = plan_slug
    return data


def create_user_session(
    db: Session,
    user_id: int,
    access_token: str,
    organization_id: Optional[int],
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> UserSession:
    session = UserSession(
        user_id=user_id,
        organization_id=organization_id,
        session_token=AuthService.hash_token(access_token),
        ip_address=ip_address,
        user_agent=user_agent,
        active=True,
    )
    db.add(session)
    return session
