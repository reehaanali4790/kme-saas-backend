"""
Create or promote the SaaS platform owner account.

Usage (Railway shell or local with DATABASE_URL):
  cd backend
  PLATFORM_ADMIN_EMAILS=owner@example.com \\
  PLATFORM_ADMIN_PASSWORD='your-secure-password' \\
  python scripts/create_platform_owner.py

Optional:
  PLATFORM_ADMIN_USERNAME=platform-owner
  PLATFORM_ADMIN_FULL_NAME='Your Name'
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_PRODUCTION_CHECKS", "true")

from config.database import SessionLocal
from modules.platform.bootstrap import ensure_platform_owner_user, sync_platform_admin_emails
from modules.tenants.provision import provision_default_tenant_if_missing, create_platform_and_shared_tables


def main():
    db = SessionLocal()
    try:
        create_platform_and_shared_tables(db)
        org = provision_default_tenant_if_missing(db)
        user = ensure_platform_owner_user(db, org)
        if not user:
            print("ERROR: Set PLATFORM_ADMIN_EMAILS and PLATFORM_ADMIN_PASSWORD")
            sys.exit(1)
        sync_platform_admin_emails(db)
        print("OK platform owner ready")
        print(f"  username: {user.username}")
        print(f"  email:    {user.email}")
        print(f"  admin:    {user.is_platform_admin}")
        print(f"  org:      {org.slug if org else 'none'}")
        print("\nSign in at your frontend /login, then open /platform/organizations")
    finally:
        db.close()


if __name__ == "__main__":
    main()
