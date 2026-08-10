"""
Tenant context resolution and database session scoping for schema-per-tenant SaaS.
"""

from dataclasses import dataclass
from typing import Generator, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from config.database import SessionLocal, set_platform_search_path, set_tenant_search_path
from models.platform_models import Organization, OrganizationMembership, User, PLATFORM_SCHEMA

from modules.auth.dependencies import get_current_user

SHARED_SCHEMA = "shared"
DEFAULT_TENANT_SCHEMA = "tenant_default"


@dataclass
class TenantContext:
    organization_id: int
    slug: str
    schema_name: str
    name: str
    role_name: str
    plan_slug: Optional[str] = None

    @property
    def redis_prefix(self) -> str:
        return self.schema_name

    @property
    def upload_prefix(self) -> str:
        return self.schema_name


def tenant_schema_name(slug: str) -> str:
    safe = slug.lower().replace("-", "_")
    if not safe.replace("_", "").isalnum():
        raise ValueError(f"Invalid tenant slug: {slug}")
    return f"tenant_{safe}"


def _tenant_context_from_payload(payload: dict, db: Session) -> Optional[TenantContext]:
    org_id = payload.get("org_id")
    tenant_schema = payload.get("tenant_schema")
    role_name = payload.get("role")
    if not org_id or not tenant_schema or not role_name:
        return None

    set_platform_search_path(db)
    org = db.query(Organization).filter(
        Organization.organization_id == int(org_id),
        Organization.status.in_(("active", "trial", "pending")),
    ).first()
    if not org or org.schema_name != tenant_schema:
        return None

    plan_slug = payload.get("plan_slug")
    if not plan_slug and org.plan:
        plan_slug = org.plan.slug

    return TenantContext(
        organization_id=org.organization_id,
        slug=org.slug,
        schema_name=org.schema_name,
        name=org.name,
        role_name=role_name,
        plan_slug=plan_slug,
    )


async def get_tenant_context_optional(request: Request) -> Optional[TenantContext]:
    from modules.auth.dependencies import decode_access_token_from_request

    payload = decode_access_token_from_request(request)
    if not payload or payload.get("type") != "access":
        return None

    db = SessionLocal()
    try:
        return _tenant_context_from_payload(payload, db)
    finally:
        db.close()


async def get_tenant_context(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> TenantContext:
    from modules.auth.dependencies import decode_access_token_from_request

    payload = decode_access_token_from_request(request)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    db = SessionLocal()
    try:
        ctx = _tenant_context_from_payload(payload, db)
        if not ctx:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization context required. Select an organization.",
            )
        membership = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == current_user.user_id,
            OrganizationMembership.organization_id == ctx.organization_id,
        ).first()
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of this organization",
            )
        return ctx
    finally:
        db.close()


def get_tenant_db(
    tenant: TenantContext = Depends(get_tenant_context),
) -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        set_tenant_search_path(db, tenant.schema_name)
        yield db
    finally:
        db.close()


def list_active_tenant_contexts(db: Session) -> list[TenantContext]:
    set_platform_search_path(db)
    orgs = db.query(Organization).filter(Organization.status.in_(("active", "trial"))).all()
    results = []
    for o in orgs:
        plan_slug = o.plan.slug if o.plan else None
        results.append(
            TenantContext(
                organization_id=o.organization_id,
                slug=o.slug,
                schema_name=o.schema_name,
                name=o.name,
                role_name="ADMIN",
                plan_slug=plan_slug,
            )
        )
    return results
