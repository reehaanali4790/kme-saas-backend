"""Platform super-admin API."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from config.database import get_platform_db
from core.permissions import require_platform_admin
from models.platform_models import Organization, User

router = APIRouter(prefix="/api/platform", tags=["Platform"])


@router.get("/organizations")
def list_organizations(
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    orgs = db.query(Organization).order_by(Organization.created_at.desc()).all()
    return [
        {
            "organization_id": o.organization_id,
            "slug": o.slug,
            "name": o.name,
            "status": o.status,
            "schema_name": o.schema_name,
            "plan": o.plan.slug if o.plan else None,
        }
        for o in orgs
    ]


@router.post("/organizations/{org_id}/suspend")
def suspend_organization(
    org_id: int,
    db: Session = Depends(get_platform_db),
    admin: User = Depends(require_platform_admin()),
):
    org = db.query(Organization).filter(Organization.organization_id == org_id).first()
    if not org:
        return {"error": "not found"}
    org.status = "suspended"
    db.commit()
    return {"status": org.status}
