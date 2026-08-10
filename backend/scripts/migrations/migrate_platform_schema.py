"""Bootstrap platform + shared + default tenant schemas."""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def main():
    db = SessionLocal()
    try:
        ensure_platform_and_shared_schemas(db)
        create_platform_and_shared_tables(db)
        seed_platform_plans(db)
        org = provision_default_tenant_if_missing(db)
        create_tenant_tables(db, org.schema_name)
        seed_tenant_defaults(db, org.schema_name, org.name)
        print(f"Platform schema: {PLATFORM_SCHEMA}")
        print(f"Shared schema: {SHARED_SCHEMA}")
        print(f"Default tenant: {org.slug} -> {org.schema_name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
