"""
Admin Endpoints — tenant-scoped user management via organization memberships.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import desc
from sqlalchemy.orm import Session

from config.database import get_platform_db
from core.permissions import require_permission
from core.schemas import PaginatedResponse, PaginationParams
from core.tenant import get_tenant_db, TenantContext, get_tenant_context
from models.database_models import Role
from models.platform_models import User, UserSession, OrganizationMembership, Organization
from modules.auth.dependencies import get_current_user
from modules.auth.services import AuthService
from infrastructure.audit.audit_service import log_audit

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _resolve_role_name(
    tenant_db: Session,
    *,
    role_name: Optional[str] = None,
    role_id: Optional[int] = None,
) -> str:
    """Accept role_name or role_id from clients; always return canonical role_name."""
    if role_name and role_name.strip():
        name = role_name.strip()
        if tenant_db.query(Role).filter(Role.role_name == name).first():
            return name
        raise HTTPException(status_code=400, detail="Invalid role")
    if role_id is not None:
        role = tenant_db.query(Role).filter(Role.role_id == role_id).first()
        if role:
            return role.role_name
        raise HTTPException(status_code=400, detail="Invalid role")
    raise HTTPException(status_code=400, detail="role_name or role_id is required")


class CreateUserRequest(BaseModel):
    username: str
    full_name: str
    email: str
    password: str
    role_name: Optional[str] = None
    role_id: Optional[int] = None
    phone_number: Optional[str] = None
    whatsapp_number: Optional[str] = None

    @model_validator(mode="after")
    def require_role(self):
        if not self.role_name and self.role_id is None:
            raise ValueError("role_name or role_id is required")
        return self


class UpdateUserRequest(BaseModel):
    full_name: str
    email: str
    role_name: Optional[str] = None
    role_id: Optional[int] = None
    phone_number: Optional[str] = None
    whatsapp_number: Optional[str] = None
    active: bool = True

    @model_validator(mode="after")
    def require_role(self):
        if not self.role_name and self.role_id is None:
            raise ValueError("role_name or role_id is required")
        return self


class ResetPasswordRequest(BaseModel):
    new_password: str


class LoginLogItem(BaseModel):
    session_id: int
    user_id: int
    full_name: str
    username: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    login_time: Optional[str] = None
    logout_time: Optional[str] = None
    active: Optional[bool] = None


def _org_member_user_ids(platform_db: Session, org_id: int) -> list[int]:
    rows = platform_db.query(OrganizationMembership.user_id).filter(
        OrganizationMembership.organization_id == org_id
    ).all()
    return [r[0] for r in rows]


@router.get("/users")
def list_users(
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    user_ids = _org_member_user_ids(platform_db, tenant.organization_id)
    if not user_ids:
        return []

    memberships = {
        m.user_id: m
        for m in platform_db.query(OrganizationMembership).filter(
            OrganizationMembership.organization_id == tenant.organization_id,
            OrganizationMembership.user_id.in_(user_ids),
        ).all()
    }
    users = platform_db.query(User).filter(User.user_id.in_(user_ids)).order_by(User.created_at.desc()).all()
    role_map = {r.role_name: r.role_id for r in tenant_db.query(Role).all()}

    return [
        {
            "user_id": u.user_id,
            "username": u.username,
            "full_name": u.full_name,
            "email": u.email,
            "role_id": role_map.get(memberships[u.user_id].role_name),
            "role_name": memberships[u.user_id].role_name,
            "phone_number": u.phone_number,
            "whatsapp_number": u.whatsapp_number,
            "active": u.active,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "login_count": u.login_count or 0,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
        if u.user_id in memberships
    ]


@router.post("/users", status_code=201)
def create_user(
    req: CreateUserRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    role_name = _resolve_role_name(tenant_db, role_name=req.role_name, role_id=req.role_id)

    org = platform_db.query(Organization).filter(
        Organization.organization_id == tenant.organization_id
    ).first()
    if org:
        from core.plan_limits import check_user_limit
        check_user_limit(platform_db, org)

    existing = AuthService.get_user_by_username(platform_db, req.username)
    if existing:
        AuthService.add_membership(
            platform_db, existing.user_id, tenant.organization_id, role_name
        )
        return {"success": True, "user_id": existing.user_id, "message": "User added to organization."}

    user = AuthService.create_user(
        db=platform_db,
        username=req.username,
        email=req.email,
        password=req.password,
        full_name=req.full_name,
        phone_number=req.phone_number,
        whatsapp_number=req.whatsapp_number,
        created_by=current_user.user_id,
    )
    AuthService.add_membership(platform_db, user.user_id, tenant.organization_id, role_name)
    log_audit(
        tenant_db, current_user.user_id, "CREATE_USER", entity_type="USER", entity_id=user.user_id,
        new_value={"username": user.username, "email": user.email, "role_name": role_name},
        description=f"Created user '{req.username}'",
    )
    tenant_db.commit()
    return {"success": True, "user_id": user.user_id, "message": f"User '{req.username}' created."}


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    req: UpdateUserRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    membership = platform_db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.organization_id == tenant.organization_id,
    ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="User not found in organization")

    user = platform_db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    role_name = _resolve_role_name(tenant_db, role_name=req.role_name, role_id=req.role_id)

    clash = platform_db.query(User).filter(User.email == req.email, User.user_id != user_id).first()
    if clash:
        raise HTTPException(status_code=400, detail="Email already in use")

    user.full_name = req.full_name
    user.email = req.email
    user.phone_number = req.phone_number
    user.whatsapp_number = req.whatsapp_number
    user.active = req.active
    user.updated_by = current_user.user_id
    user.updated_at = datetime.utcnow()
    membership.role_name = role_name

    log_audit(
        tenant_db, current_user.user_id, "UPDATE_USER", entity_type="USER", entity_id=user.user_id,
        new_value={"full_name": req.full_name, "email": req.email, "role_name": role_name, "active": req.active},
        description=f"Updated user '{user.username}'",
    )
    platform_db.commit()
    tenant_db.commit()
    return {"success": True, "message": "User updated."}


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    req: ResetPasswordRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    membership = platform_db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.organization_id == tenant.organization_id,
    ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="User not found")

    user = platform_db.query(User).filter(User.user_id == user_id).first()
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user.password_hash = AuthService.hash_password(req.new_password)
    user.updated_by = current_user.user_id
    user.updated_at = datetime.utcnow()
    log_audit(
        tenant_db, current_user.user_id, "RESET_PASSWORD", entity_type="USER", entity_id=user.user_id,
        description=f"Password reset for user '{user.username}'",
    )
    platform_db.commit()
    tenant_db.commit()
    return {"success": True, "message": "Password reset successfully."}


@router.post("/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    membership = platform_db.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.organization_id == tenant.organization_id,
    ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="User not found")

    user = platform_db.query(User).filter(User.user_id == user_id).first()
    user.active = not user.active
    user.updated_by = current_user.user_id
    user.updated_at = datetime.utcnow()
    log_audit(
        tenant_db, current_user.user_id, "TOGGLE_USER_ACTIVE", entity_type="USER", entity_id=user.user_id,
        new_value={"active": user.active},
        description=f"User '{user.username}' {'activated' if user.active else 'deactivated'}",
    )
    platform_db.commit()
    tenant_db.commit()
    return {"success": True, "active": user.active}


@router.get("/roles")
def list_roles(
    tenant_db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    roles = tenant_db.query(Role).order_by(Role.role_id).all()
    return [
        {
            "role_id": r.role_id,
            "role_name": r.role_name,
            "role_description": r.role_description,
            "can_manage_users": r.can_manage_users,
        }
        for r in roles
    ]


@router.get("/logs", response_model=PaginatedResponse[LoginLogItem])
def get_login_logs(
    params: PaginationParams = Depends(),
    tenant: TenantContext = Depends(get_tenant_context),
    platform_db: Session = Depends(get_platform_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    query = platform_db.query(UserSession, User).join(User, UserSession.user_id == User.user_id).filter(
        UserSession.organization_id == tenant.organization_id
    )
    total = query.count()
    sessions = (
        query.order_by(desc(UserSession.login_time))
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    items = [
        {
            "session_id": s.session_id,
            "user_id": s.user_id,
            "full_name": u.full_name,
            "username": u.username,
            "ip_address": str(s.ip_address) if s.ip_address else None,
            "user_agent": s.user_agent,
            "login_time": s.login_time.isoformat() if s.login_time else None,
            "logout_time": s.logout_time.isoformat() if s.logout_time else None,
            "active": s.active,
        }
        for s, u in sessions
    ]
    return PaginatedResponse.create(items, total, params)
