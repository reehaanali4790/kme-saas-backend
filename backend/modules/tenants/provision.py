"""
Tenant schema provisioning — CREATE SCHEMA + tenant tables + default seeds.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.database import Base, SessionLocal, PLATFORM_SCHEMA, SHARED_SCHEMA, set_platform_search_path, set_tenant_search_path
from core.tenant import tenant_schema_name, DEFAULT_TENANT_SCHEMA
from models.platform_models import Organization, OrganizationMembership, Plan
from models import database_models  # noqa: F401 — register tenant models
from models import platform_models  # noqa: F401
from modules.tenants.seeds import DEFAULT_ROLES, DEFAULT_PLANS

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


def validate_slug(slug: str) -> str:
    slug = slug.lower().strip()
    if not SLUG_RE.match(slug):
        raise ValueError("Slug must be 3-50 chars, lowercase alphanumeric and hyphens")
    return slug


def ensure_platform_and_shared_schemas(db: Session) -> None:
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {PLATFORM_SCHEMA}"))
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SHARED_SCHEMA}"))
    db.commit()


def seed_platform_plans(db: Session) -> None:
    from config.settings import settings

    stripe_map = {
        "operations": {
            "monthly": settings.STRIPE_PRICE_OPS_MONTHLY,
            "annual": settings.STRIPE_PRICE_OPS_ANNUAL,
        },
        "trade-desk": {
            "monthly": settings.STRIPE_PRICE_TD_MONTHLY,
            "annual": settings.STRIPE_PRICE_TD_ANNUAL,
        },
    }

    set_platform_search_path(db)
    for plan_data in DEFAULT_PLANS:
        slug = plan_data["slug"]
        prices = stripe_map.get(slug, {})
        payload = dict(plan_data)
        if prices.get("monthly"):
            payload["stripe_price_monthly_id"] = prices["monthly"]
        if prices.get("annual"):
            payload["stripe_price_annual_id"] = prices["annual"]

        existing = db.query(Plan).filter(Plan.slug == slug).first()
        if existing:
            if prices.get("monthly") and not existing.stripe_price_monthly_id:
                existing.stripe_price_monthly_id = prices["monthly"]
            if prices.get("annual") and not existing.stripe_price_annual_id:
                existing.stripe_price_annual_id = prices["annual"]
            continue
        db.add(Plan(**payload))
    db.commit()


def create_platform_and_shared_tables(db: Session) -> None:
    ensure_platform_and_shared_schemas(db)
    platform_tables = [t for t in Base.metadata.tables.values() if t.schema == PLATFORM_SCHEMA]
    shared_tables = [t for t in Base.metadata.tables.values() if t.schema == SHARED_SCHEMA]
    conn = db.connection()
    Base.metadata.create_all(bind=conn, tables=platform_tables, checkfirst=True)
    Base.metadata.create_all(bind=conn, tables=shared_tables, checkfirst=True)
    db.commit()


def create_tenant_tables(db: Session, schema_name: str) -> None:
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))
    db.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    db.commit()
    set_tenant_search_path(db, schema_name)

    tenant_tables = [
        t for t in Base.metadata.tables.values()
        if t.schema is None or t.schema not in (PLATFORM_SCHEMA, SHARED_SCHEMA)
    ]
    Base.metadata.create_all(bind=db.connection(), tables=tenant_tables, checkfirst=True)
    db.commit()


def seed_tenant_defaults(db: Session, schema_name: str, app_name: str = "ISM") -> None:
    from models.database_models import Role, BrandingConfig, DemurrageConfig

    set_tenant_search_path(db, schema_name)

    for role_data in DEFAULT_ROLES:
        exists = db.query(Role).filter(Role.role_name == role_data["role_name"]).first()
        if not exists:
            db.add(Role(**role_data))

    branding = db.query(BrandingConfig).filter(BrandingConfig.config_id == 1).first()
    if not branding:
        db.add(BrandingConfig(config_id=1, app_name=app_name))

    demurrage = db.query(DemurrageConfig).filter(DemurrageConfig.config_id == 1).first()
    if not demurrage:
        db.add(DemurrageConfig(config_id=1))

    db.commit()


def provision_tenant(
    db: Session,
    slug: str,
    name: str,
    plan_slug: str = "operations",
    status: str = "active",
    trial_days: int = 14,
) -> Organization:
    slug = validate_slug(slug)
    schema_name = tenant_schema_name(slug)

    ensure_platform_and_shared_schemas(db)
    create_platform_and_shared_tables(db)
    seed_platform_plans(db)

    set_platform_search_path(db)
    existing = db.query(Organization).filter(
        (Organization.slug == slug) | (Organization.schema_name == schema_name)
    ).first()
    if existing:
        raise ValueError(f"Organization already exists: {slug}")

    plan = db.query(Plan).filter(Plan.slug == plan_slug).first()
    if not plan:
        raise ValueError(f"Plan not found: {plan_slug}")

    org = Organization(
        slug=slug,
        name=name,
        schema_name=schema_name,
        status=status,
        plan_id=plan.plan_id,
        trial_ends_at=datetime.utcnow() + timedelta(days=trial_days) if status == "trial" else None,
    )
    db.add(org)
    db.flush()

    create_tenant_tables(db, schema_name)
    seed_tenant_defaults(db, schema_name, app_name=name)

    db.commit()
    db.refresh(org)
    logger.info("Provisioned tenant %s -> %s", slug, schema_name)
    return org


def provision_default_tenant_if_missing(db: Session) -> Optional[Organization]:
    set_platform_search_path(db)
    org = db.query(Organization).filter(Organization.schema_name == DEFAULT_TENANT_SCHEMA).first()
    if org:
        return org
    return provision_tenant(db, slug="default", name="Default Organization", plan_slug="enterprise", status="active", trial_days=0)


def suspend_tenant(db: Session, organization_id: int) -> Organization:
    set_platform_search_path(db)
    org = db.query(Organization).filter(Organization.organization_id == organization_id).first()
    if not org:
        raise ValueError("Organization not found")
    org.status = "suspended"
    db.commit()
    return org


def run_cli():
    import argparse

    parser = argparse.ArgumentParser(description="Provision a tenant schema")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--plan", default="operations")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        org = provision_tenant(db, slug=args.slug, name=args.name, plan_slug=args.plan)
        print(f"OK organization_id={org.organization_id} schema={org.schema_name}")
    finally:
        db.close()


if __name__ == "__main__":
    run_cli()
