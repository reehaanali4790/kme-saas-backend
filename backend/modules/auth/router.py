"""
Authentication API — multi-tenant JWT with organization context.
"""

import ipaddress
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from config.database import get_platform_db, set_tenant_search_path, SessionLocal
from config.settings import settings
from core.rate_limit import limiter
from core.tenant import TenantContext, get_tenant_db
from models.platform_models import User, UserSession, Organization, OrganizationMembership
from modules.auth.dependencies import (
    build_token_payload,
    create_user_session,
    decode_access_token_from_request,
    get_current_user,
    resolve_user_permissions,
)
from modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    OrganizationSummary,
    RefreshRequest,
    SelectOrgRequest,
    Token,
    UserResponse,
)
from modules.auth.services import AuthService
from infrastructure.audit.audit_service import log_audit

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _get_client_ip(request: Request) -> Optional[str]:
    if not request.client or not request.client.host:
        return None
    ip_str = request.client.host
    try:
        ipaddress.ip_address(ip_str)
        return ip_str
    except ValueError:
        if ip_str == "testclient":
            return "127.0.0.1"
        return None


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> str:
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="strict",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="strict",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        secure=settings.ENVIRONMENT == "production",
        samesite="strict",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return csrf_token


def _issue_tokens_for_membership(
    db: Session,
    user: User,
    membership: OrganizationMembership,
    request: Request,
    response: Response,
) -> dict:
    org = db.query(Organization).filter(
        Organization.organization_id == membership.organization_id
    ).first()
    if not org:
        raise HTTPException(status_code=400, detail="Organization not found")

    plan_slug = org.plan.slug if org.plan else None
    token_data = build_token_payload(
        user=user,
        org_id=org.organization_id,
        tenant_schema=org.schema_name,
        role_name=membership.role_name,
        plan_slug=plan_slug,
    )
    access_token = AuthService.create_access_token(data=token_data)
    refresh_token = AuthService.create_refresh_token(data={"sub": user.user_id, "type": "refresh"})
    csrf_token = _set_auth_cookies(response, access_token, refresh_token)

    create_user_session(
        db=db,
        user_id=user.user_id,
        access_token=access_token,
        organization_id=org.organization_id,
        ip_address=_get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    tenant_db = SessionLocal()
    try:
        set_tenant_search_path(tenant_db, org.schema_name)
        user_payload = AuthService.serialize_user_payload(user, membership, org, tenant_db)
    finally:
        tenant_db.close()

    memberships = AuthService.get_user_memberships(db, user.user_id)
    organizations = []
    for m in memberships:
        o = db.query(Organization).filter(Organization.organization_id == m.organization_id).first()
        if o:
            organizations.append(
                OrganizationSummary(
                    org_id=o.organization_id,
                    slug=o.slug,
                    name=o.name,
                    role=m.role_name,
                    plan=o.plan.slug if o.plan else None,
                )
            )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "csrf_token": csrf_token,
        "token_type": "bearer",
        "user": user_payload,
        "organizations": [o.model_dump() for o in organizations],
        "requires_org_selection": False,
    }


@router.post("/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_platform_db),
):
    user = AuthService.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        log_audit(db, None, "LOGIN_FAILED", entity_type="USER",
                  description=f"Failed login attempt for username '{form_data.username}'",
                  ip_address=_get_client_ip(request))
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    memberships = AuthService.get_user_memberships(db, user.user_id)
    if not memberships:
        raise HTTPException(status_code=403, detail="User has no organization memberships")

    organizations = []
    for m in memberships:
        org = db.query(Organization).filter(Organization.organization_id == m.organization_id).first()
        if org:
            organizations.append(
                OrganizationSummary(
                    org_id=org.organization_id,
                    slug=org.slug,
                    name=org.name,
                    role=m.role_name,
                    plan=org.plan.slug if org.plan else None,
                )
            )

    if len(memberships) > 1:
        pre_token = AuthService.create_access_token(
            data={"sub": user.user_id, "username": user.username, "type": "pre_auth"},
            expires_delta=__import__("datetime").timedelta(minutes=15),
        )
        return {
            "access_token": pre_token,
            "refresh_token": "",
            "token_type": "bearer",
            "requires_org_selection": True,
            "organizations": [o.model_dump() for o in organizations],
            "user": {
                "user_id": user.user_id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "is_platform_admin": bool(user.is_platform_admin),
            },
        }

    membership = AuthService.get_default_membership(db, user.user_id)
    result = _issue_tokens_for_membership(db, user, membership, request, response)
    log_audit(db, user.user_id, "LOGIN", entity_type="USER", entity_id=user.user_id,
              description=f"User '{user.username}' logged in",
              ip_address=_get_client_ip(request))
    db.commit()
    return result


@router.post("/select-org", response_model=Token)
@limiter.limit(settings.LOGIN_RATE_LIMIT)
def select_org(
    request: Request,
    response: Response,
    body: SelectOrgRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_platform_db),
):
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == current_user.user_id,
        OrganizationMembership.organization_id == body.org_id,
    ).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    result = _issue_tokens_for_membership(db, current_user, membership, request, response)
    log_audit(db, current_user.user_id, "SELECT_ORG", entity_type="ORGANIZATION",
              entity_id=body.org_id, description=f"Selected org {body.org_id}",
              ip_address=_get_client_ip(request))
    db.commit()
    return result


@router.post("/switch-org", response_model=Token)
def switch_org(
    request: Request,
    response: Response,
    body: SelectOrgRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_platform_db),
):
    return select_org(request, response, body, current_user, db)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_platform_db),
):
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if token:
        session = db.query(UserSession).filter(
            UserSession.session_token == AuthService.hash_token(token),
            UserSession.active == True,
        ).first()
        if session:
            session.active = False
            session.logout_time = datetime.utcnow()
            log_audit(db, current_user.user_id, "LOGOUT", entity_type="USER",
                      entity_id=current_user.user_id,
                      description=f"User '{current_user.username}' logged out",
                      ip_address=_get_client_ip(request))
            db.commit()

        from core.redis import redis_cache
        payload = AuthService.decode_token(token) or {}
        org_id = payload.get("org_id") or "none"
        try:
            redis_cache.delete(f"lme:session_active:{org_id}:{AuthService.hash_token(token)}")
        except Exception:
            pass

    response.delete_cookie(key="access_token", httponly=True, secure=settings.ENVIRONMENT == "production", samesite="strict")
    response.delete_cookie(key="refresh_token", httponly=True, secure=settings.ENVIRONMENT == "production", samesite="strict")
    return {"message": "Logged out successfully"}


@router.get("/me")
def get_current_user_info(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
):
    payload = decode_access_token_from_request(request)
    org_id = payload.get("org_id") if payload else None
    role_name = payload.get("role") if payload else None

    membership = None
    org = None
    if org_id:
        membership = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == current_user.user_id,
            OrganizationMembership.organization_id == int(org_id),
        ).first()
        org = db.query(Organization).filter(Organization.organization_id == int(org_id)).first()

    permissions = resolve_user_permissions(tenant_db, role_name) if role_name else {}

    return {
        "user_id": current_user.user_id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role_name": role_name or membership.role_name if membership else "VIEWER",
        "active": current_user.active,
        "permissions": permissions,
        "organization": {
            "org_id": org.organization_id,
            "slug": org.slug,
            "name": org.name,
            "plan": org.plan.slug if org and org.plan else None,
        } if org else None,
        "is_platform_admin": current_user.is_platform_admin,
    }


@router.post("/change-password")
def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_platform_db),
):
    if not AuthService.verify_password(request.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect old password")
    if len(request.new_password) < settings.PWD_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {settings.PWD_MIN_LENGTH} characters",
        )
    current_user.password_hash = AuthService.hash_password(request.new_password)
    current_user.updated_at = datetime.utcnow()
    log_audit(db, current_user.user_id, "CHANGE_PASSWORD", entity_type="USER",
              entity_id=current_user.user_id,
              description=f"User '{current_user.username}' changed their own password")
    db.commit()
    return {"message": "Password changed successfully"}


@router.get("/check-permission/{permission}")
def check_permission(
    permission: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    tenant_db: Session = Depends(get_tenant_db),
):
    payload = decode_access_token_from_request(request)
    role_name = payload.get("role") if payload else None
    has_permission = AuthService.check_permission_for_role(tenant_db, role_name, permission) if role_name else False
    return {"permission": permission, "granted": has_permission, "role": role_name}


@router.post("/refresh")
@limiter.limit(settings.REFRESH_RATE_LIMIT)
def refresh_token(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = None,
    db: Session = Depends(get_platform_db),
):
    refresh_token_val = body.refresh_token if body and body.refresh_token else request.cookies.get("refresh_token")
    if not refresh_token_val:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    payload = AuthService.decode_token(refresh_token_val)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id_val = payload.get("sub")
    if user_id_val is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = AuthService.get_user_by_id(db, int(user_id_val))
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    current_payload = decode_access_token_from_request(request)
    membership = None
    if current_payload and current_payload.get("org_id"):
        membership = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user.user_id,
            OrganizationMembership.organization_id == int(current_payload["org_id"]),
        ).first()
    if not membership:
        membership = AuthService.get_default_membership(db, user.user_id)
    if not membership:
        raise HTTPException(status_code=403, detail="No organization membership")

    org = db.query(Organization).filter(Organization.organization_id == membership.organization_id).first()
    plan_slug = org.plan.slug if org and org.plan else None
    token_data = build_token_payload(
        user=user,
        org_id=org.organization_id,
        tenant_schema=org.schema_name,
        role_name=membership.role_name,
        plan_slug=plan_slug,
    )
    new_access_token = AuthService.create_access_token(data=token_data)
    response.set_cookie(
        key="access_token",
        value=new_access_token,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="strict",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    create_user_session(
        db=db,
        user_id=user.user_id,
        access_token=new_access_token,
        organization_id=org.organization_id,
        ip_address=_get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return {"access_token": new_access_token, "token_type": "bearer"}
