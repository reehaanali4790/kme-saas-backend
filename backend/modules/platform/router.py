"""Platform super-admin API — owner / operator console."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.database import get_platform_db
from core.permissions import require_platform_admin
from models.platform_models import Organization, OrganizationMembership, Plan, User

router = APIRouter(prefix="/api/platform", tags=["Platform"])


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
        {
            "organization_id": o.organization_id,
            "slug": o.slug,
            "name": o.name,
            "status": o.status,
            "schema_name": o.schema_name,
            "plan": o.plan.slug if o.plan else None,
            "plan_name": o.plan.name if o.plan else None,
            "trial_ends_at": o.trial_ends_at.isoformat() if o.trial_ends_at else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "member_count": int(member_counts.get(o.organization_id, 0)),
        }
        for o in orgs
    ]


@router.post("/organizations/{org_id}/suspend")
def suspend_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = _get_org(db, org_id)
    org.status = "suspended"
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
    db.commit()
    return {"organization_id": org.organization_id, "status": org.status}


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
    db.commit()
    return {
        "organization_id": org.organization_id,
        "plan": plan.slug,
        "plan_name": plan.name,
    }


def _get_org(db: Session, org_id: int) -> Organization:
    org = db.query(Organization).filter(Organization.organization_id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org
