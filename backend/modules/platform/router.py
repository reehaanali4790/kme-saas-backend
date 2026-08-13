"""Platform super-admin API — owner / operator console."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.database import get_platform_db
from config.settings import settings
from core.permissions import require_platform_admin
from infrastructure.audit.audit_service import log_platform_audit
from models.platform_models import Organization, OrganizationMembership, Plan, User
from modules.auth.services import AuthService
from modules.tenants.provision import provision_tenant, validate_slug

router = APIRouter(prefix="/api/platform", tags=["Platform"])


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(min_length=3, max_length=50)
    plan_slug: str = "operations"
    status: str = "active"
    admin_username: Optional[str] = None
    admin_email: Optional[EmailStr] = None
    admin_password: Optional[str] = None
    admin_full_name: Optional[str] = None


def _org_summary(o: Organization, member_count: int = 0) -> dict:
    return {
        "organization_id": o.organization_id,
        "slug": o.slug,
        "name": o.name,
        "status": o.status,
        "schema_name": o.schema_name,
        "plan": o.plan.slug if o.plan else None,
        "plan_name": o.plan.name if o.plan else None,
        "trial_ends_at": o.trial_ends_at.isoformat() if o.trial_ends_at else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "member_count": member_count,
    }


def _get_org(db: Session, org_id: int) -> Organization:
    org = db.query(Organization).filter(Organization.organization_id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.get("/me")
def platform_me(admin: User = Depends(require_platform_admin())):
    return {
        "is_platform_admin": True,
        "user_id": admin.user_id,
        "email": admin.email,
        "full_name": admin.full_name,
    }


@router.get("/overview")
def platform_overview(
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    orgs = db.query(Organization).all()
    by_status: dict[str, int] = {}
    for o in orgs:
        by_status[o.status] = by_status.get(o.status, 0) + 1

    total_users = db.query(func.count(User.user_id)).scalar() or 0
    total_memberships = db.query(func.count(OrganizationMembership.membership_id)).scalar() or 0

    return {
        "organizations_total": len(orgs),
        "organizations_by_status": by_status,
        "users_total": total_users,
        "memberships_total": total_memberships,
    }


@router.get("/organizations")
def list_organizations(
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    orgs = db.query(Organization).order_by(Organization.created_at.desc()).all()
    member_counts = dict(
        db.query(OrganizationMembership.organization_id, func.count(OrganizationMembership.membership_id))
        .group_by(OrganizationMembership.organization_id)
        .all()
    )
    return [
        _org_summary(o, int(member_counts.get(o.organization_id, 0)))
        for o in orgs
    ]


@router.post("/organizations")
def create_organization(
    body: CreateOrganizationRequest,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    try:
        slug = validate_slug(body.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    status = (body.status or "active").strip().lower()
    if status not in ("active", "trial"):
        raise HTTPException(status_code=400, detail="status must be active or trial")

    admin_fields = [body.admin_username, body.admin_email, body.admin_password, body.admin_full_name]
    creating_admin = any(admin_fields)
    if creating_admin and not all(admin_fields):
        raise HTTPException(
            status_code=400,
            detail="Provide admin_username, admin_email, admin_password, and admin_full_name together",
        )

    if creating_admin:
        if AuthService.get_user_by_username(db, body.admin_username):
            raise HTTPException(status_code=400, detail="Admin username already exists")
        if AuthService.get_user_by_email(db, str(body.admin_email)):
            raise HTTPException(status_code=400, detail="Admin email already exists")
        if len(body.admin_password or "") < settings.PWD_MIN_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Admin password must be at least {settings.PWD_MIN_LENGTH} characters",
            )

    try:
        org = provision_tenant(
            db,
            slug=slug,
            name=body.name.strip(),
            plan_slug=body.plan_slug,
            status=status,
            trial_days=14 if status == "trial" else 0,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    org_admin_payload = None
    if creating_admin:
        org_admin = AuthService.create_user(
            db,
            username=body.admin_username.strip(),
            email=str(body.admin_email).strip().lower(),
            password=body.admin_password,
            full_name=body.admin_full_name.strip(),
            is_platform_admin=False,
        )
        AuthService.add_membership(
            db,
            org_admin.user_id,
            org.organization_id,
            "ADMIN",
            is_default=True,
            invited_by=admin.user_id,
        )
        org_admin_payload = {
            "user_id": org_admin.user_id,
            "username": org_admin.username,
            "email": org_admin.email,
            "full_name": org_admin.full_name,
            "role": "ADMIN",
        }

    # Platform owner can Open workspace for support
    AuthService.add_membership(
        db,
        admin.user_id,
        org.organization_id,
        "ADMIN",
        is_default=False,
        invited_by=admin.user_id,
    )

    log_platform_audit(
        db,
        admin.user_id,
        "CREATE_ORGANIZATION",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Created organization '{org.name}' ({org.slug})",
    )
    db.commit()
    db.refresh(org)

    member_count = (
        db.query(func.count(OrganizationMembership.membership_id))
        .filter(OrganizationMembership.organization_id == org.organization_id)
        .scalar()
        or 0
    )

    return {
        **_org_summary(org, int(member_count)),
        "org_admin": org_admin_payload,
    }


@router.get("/organizations/{org_id}")
def get_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = _get_org(db, org_id)
    memberships = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.organization_id == org_id)
        .all()
    )
    members = []
    for m in memberships:
        user = db.query(User).filter(User.user_id == m.user_id).first()
        if not user:
            continue
        members.append(
            {
                "user_id": user.user_id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "role_name": m.role_name,
                "is_platform_admin": bool(user.is_platform_admin),
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            }
        )

    return {
        **_org_summary(org, len(members)),
        "members": members,
    }


@router.post("/organizations/{org_id}/suspend")
def suspend_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = _get_org(db, org_id)
    org.status = "suspended"
    org.updated_at = datetime.utcnow()
    log_platform_audit(
        db,
        admin.user_id,
        "SUSPEND_ORGANIZATION",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Suspended organization '{org.name}'",
    )
    db.commit()
    return {"organization_id": org.organization_id, "status": org.status}


@router.post("/organizations/{org_id}/activate")
def activate_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = _get_org(db, org_id)
    org.status = "active"
    org.updated_at = datetime.utcnow()
    log_platform_audit(
        db,
        admin.user_id,
        "ACTIVATE_ORGANIZATION",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Activated organization '{org.name}'",
    )
    db.commit()
    return {"organization_id": org.organization_id, "status": org.status}


@router.post("/organizations/{org_id}/archive")
def archive_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = _get_org(db, org_id)
    org.status = "archived"
    org.updated_at = datetime.utcnow()
    log_platform_audit(
        db,
        admin.user_id,
        "ARCHIVE_ORGANIZATION",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Archived organization '{org.name}'",
    )
    db.commit()
    return {"organization_id": org.organization_id, "status": org.status}


@router.post("/organizations/{org_id}/grant-access")
def grant_platform_access(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    """Ensure the current platform admin has ADMIN membership so they can Open workspace."""
    org = _get_org(db, org_id)
    if org.status not in ("active", "trial", "pending"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot open workspace for organization with status '{org.status}'",
        )

    membership = AuthService.add_membership(
        db,
        admin.user_id,
        org.organization_id,
        "ADMIN",
        is_default=False,
        invited_by=admin.user_id,
    )
    # Ensure role is ADMIN if membership already existed with a weaker role
    if membership.role_name != "ADMIN":
        membership.role_name = "ADMIN"
        db.commit()
        db.refresh(membership)

    log_platform_audit(
        db,
        admin.user_id,
        "GRANT_PLATFORM_ACCESS",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Granted platform owner access to '{org.name}'",
    )
    db.commit()

    return {
        "organization_id": org.organization_id,
        "org_id": org.organization_id,
        "slug": org.slug,
        "name": org.name,
        "role": membership.role_name,
        "membership_id": membership.membership_id,
    }


@router.patch("/organizations/{org_id}/plan")
def update_organization_plan(
    org_id: int,
    body: dict,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    plan_slug = body.get("plan_slug")
    if not plan_slug:
        raise HTTPException(status_code=400, detail="plan_slug required")

    org = _get_org(db, org_id)
    plan = db.query(Plan).filter(Plan.slug == plan_slug).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan")

    org.plan_id = plan.plan_id
    org.updated_at = datetime.utcnow()
    log_platform_audit(
        db,
        admin.user_id,
        "UPDATE_ORGANIZATION_PLAN",
        entity_type="ORGANIZATION",
        entity_id=org.organization_id,
        organization_id=org.organization_id,
        description=f"Changed plan for '{org.name}' to {plan.slug}",
    )
    db.commit()
    return {
        "organization_id": org.organization_id,
        "plan": plan.slug,
        "plan_name": plan.name,
    }
