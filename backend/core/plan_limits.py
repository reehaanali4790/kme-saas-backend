"""Plan limit enforcement for SaaS tiers."""

from datetime import date
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from config.database import get_platform_db
from core.tenant import TenantContext, get_tenant_context
from models.platform_models import Organization, Plan, UsageCounter, OrganizationMembership


def get_organization_plan(db: Session, org: Organization) -> Optional[Plan]:
    if org.plan_id:
        return db.query(Plan).filter(Plan.plan_id == org.plan_id).first()
    return None


def get_or_create_usage_counter(db: Session, org_id: int) -> UsageCounter:
    period_start = date.today().replace(day=1)
    counter = db.query(UsageCounter).filter(
        UsageCounter.organization_id == org_id,
        UsageCounter.period_start == period_start,
    ).first()
    if not counter:
        counter = UsageCounter(organization_id=org_id, period_start=period_start)
        db.add(counter)
        db.flush()
    return counter


def check_user_limit(db: Session, org: Organization) -> None:
    plan = get_organization_plan(db, org)
    if not plan or plan.max_users is None:
        return
    count = db.query(OrganizationMembership).filter(
        OrganizationMembership.organization_id == org.organization_id
    ).count()
    if count >= plan.max_users:
        raise HTTPException(status_code=403, detail="Plan user limit reached. Upgrade to add more users.")


def check_document_limit(db: Session, org: Organization) -> None:
    plan = get_organization_plan(db, org)
    if not plan or plan.max_documents_per_month is None:
        return
    counter = get_or_create_usage_counter(db, org.organization_id)
    if counter.documents_uploaded >= plan.max_documents_per_month:
        raise HTTPException(
            status_code=403,
            detail="Monthly document limit reached. Upgrade your plan or wait until next month.",
        )


def increment_document_usage(db: Session, org_id: int, count: int = 1) -> None:
    counter = get_or_create_usage_counter(db, org_id)
    counter.documents_uploaded = (counter.documents_uploaded or 0) + count
    db.flush()


def require_plan_feature(feature: str):
    def _check(
        tenant: TenantContext = Depends(get_tenant_context),
        db: Session = Depends(get_platform_db),
    ) -> TenantContext:
        org = db.query(Organization).filter(Organization.organization_id == tenant.organization_id).first()
        plan = get_organization_plan(db, org) if org else None
        flags = (plan.feature_flags or {}) if plan else {}
        if not flags.get(feature):
            raise HTTPException(
                status_code=403,
                detail=f"Feature '{feature}' is not included in your current plan.",
            )
        return tenant

    return _check
