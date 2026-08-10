"""
Migrate legacy single-schema deployment to schema-per-tenant layout.
Run once on existing production databases before deploying multi-tenant code.
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_PRODUCTION_CHECKS", "true")

from sqlalchemy import text
from config.database import SessionLocal, PLATFORM_SCHEMA, SHARED_SCHEMA
from modules.tenants.provision import (
    ensure_platform_and_shared_schemas,
    create_platform_and_shared_tables,
    seed_platform_plans,
    provision_default_tenant_if_missing,
    create_tenant_tables,
    seed_tenant_defaults,
)
from models.platform_models import Organization, OrganizationMembership, User
from modules.auth.services import AuthService


SHARED_TABLES = (
    "lme_bulletins", "lme_prices", "lme_price_history", "lme_sync_runs",
    "lme_bulletin_crawl_log", "currency_rates",
)


def move_table_to_schema(db, table: str, target_schema: str):
    db.execute(text(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{table}'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = '{target_schema}' AND table_name = '{table}'
            ) THEN
                EXECUTE 'ALTER TABLE public.{table} SET SCHEMA {target_schema}';
            END IF;
        END $$;
    """))
    db.commit()


def migrate_public_business_tables(db, tenant_schema: str):
    rows = db.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)).fetchall()
    for (table_name,) in rows:
        if table_name in SHARED_TABLES:
            continue
        move_table_to_schema(db, table_name, tenant_schema)


def backfill_memberships(db, org_id: int):
    users = db.query(User).all()
    for user in users:
        existing = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user.user_id,
            OrganizationMembership.organization_id == org_id,
        ).first()
        if not existing:
            AuthService.add_membership(db, user.user_id, org_id, "ADMIN", is_default=True)


def main():
    db = SessionLocal()
    try:
        ensure_platform_and_shared_schemas(db)
        create_platform_and_shared_tables(db)
        seed_platform_plans(db)

        org = provision_default_tenant_if_missing(db)
        tenant_schema = org.schema_name

        for t in SHARED_TABLES:
            move_table_to_schema(db, t, SHARED_SCHEMA)

        migrate_public_business_tables(db, tenant_schema)

        # If tenant schema empty, create tables there
        create_tenant_tables(db, tenant_schema)
        seed_tenant_defaults(db, tenant_schema, org.name)

        # Migrate users from tenant.users to platform if still in public/tenant
        move_table_to_schema(db, "users", PLATFORM_SCHEMA)
        move_table_to_schema(db, "user_sessions", PLATFORM_SCHEMA)

        backfill_memberships(db, org.organization_id)
        print(f"Migration complete. Default org: {org.slug} schema={tenant_schema}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
