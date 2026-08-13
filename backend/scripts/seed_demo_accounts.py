"""
Seed SaaS platform admin + demo company org admin accounts.

Creates:
  1) Platform owner  — sees all companies at /platform/organizations
  2) Demo company org + org admin — uses the product as a customer
  3) Second sample org (optional) — so the platform console has more than one company

Usage (from backend/ with DATABASE_URL):
  python scripts/seed_demo_accounts.py

Optional env overrides:
  DEMO_PLATFORM_USERNAME / DEMO_PLATFORM_PASSWORD / DEMO_PLATFORM_EMAIL
  DEMO_ORG_USERNAME / DEMO_ORG_PASSWORD / DEMO_ORG_EMAIL
  DEMO_ORG_SLUG / DEMO_ORG_NAME
  DEMO_ORG2_SLUG / DEMO_ORG2_NAME / DEMO_ORG2_USERNAME / DEMO_ORG2_PASSWORD / DEMO_ORG2_EMAIL
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_PRODUCTION_CHECKS", "true")

from sqlalchemy.orm import Session

from config.database import SessionLocal
from config.settings import settings
from models.platform_models import Organization, User
from modules.auth.services import AuthService
from modules.tenants.provision import (
    create_platform_and_shared_tables,
    provision_default_tenant_if_missing,
    provision_tenant,
)


# ── Default demo credentials (local / pitch only — change in production) ─────
PLATFORM = {
    "username": os.getenv("DEMO_PLATFORM_USERNAME", "platform-owner"),
    "email": os.getenv("DEMO_PLATFORM_EMAIL", "platform@lme-saas.local"),
    "password": os.getenv("DEMO_PLATFORM_PASSWORD", "PlatformOwner123!"),
    "full_name": os.getenv("DEMO_PLATFORM_FULL_NAME", "SaaS Platform Owner"),
}

ORG1 = {
    "slug": os.getenv("DEMO_ORG_SLUG", "acme-metals"),
    "name": os.getenv("DEMO_ORG_NAME", "Acme Metals Trading"),
    "plan": os.getenv("DEMO_ORG_PLAN", "operations"),
    "username": os.getenv("DEMO_ORG_USERNAME", "acme-admin"),
    "email": os.getenv("DEMO_ORG_EMAIL", "admin@acme-metals.local"),
    "password": os.getenv("DEMO_ORG_PASSWORD", "AcmeAdmin123!"),
    "full_name": os.getenv("DEMO_ORG_FULL_NAME", "Acme Org Admin"),
}

ORG2 = {
    "slug": os.getenv("DEMO_ORG2_SLUG", "karachi-copper"),
    "name": os.getenv("DEMO_ORG2_NAME", "Karachi Copper Importers"),
    "plan": os.getenv("DEMO_ORG2_PLAN", "trade-desk"),
    "username": os.getenv("DEMO_ORG2_USERNAME", "kc-admin"),
    "email": os.getenv("DEMO_ORG2_EMAIL", "admin@karachi-copper.local"),
    "password": os.getenv("DEMO_ORG2_PASSWORD", "KcAdmin123!"),
    "full_name": os.getenv("DEMO_ORG2_FULL_NAME", "Karachi Copper Admin"),
}


def _ensure_user(
    db: Session,
    *,
    username: str,
    email: str,
    password: str,
    full_name: str,
    is_platform_admin: bool = False,
) -> tuple[User, bool]:
    """Return (user, created). Resets password if user already exists (demo seed only)."""
    user = (
        db.query(User)
        .filter((User.username == username) | (User.email.ilike(email)))
        .first()
    )
    if user:
        user.password_hash = AuthService.hash_password(password)
        user.full_name = full_name
        user.email = email
        user.username = username
        user.is_platform_admin = is_platform_admin
        user.active = True
        db.commit()
        db.refresh(user)
        return user, False

    user = AuthService.create_user(
        db,
        username=username,
        email=email,
        password=password,
        full_name=full_name,
        is_platform_admin=is_platform_admin,
    )
    return user, True


def _ensure_org(db: Session, slug: str, name: str, plan: str) -> Organization:
    existing = db.query(Organization).filter(Organization.slug == slug).first()
    if existing:
        return existing
    return provision_tenant(db, slug=slug, name=name, plan_slug=plan, status="active", trial_days=0)


def _ensure_membership(db: Session, user: User, org: Organization, role: str = "ADMIN") -> None:
    memberships = AuthService.get_user_memberships(db, user.user_id)
    is_default = len(memberships) == 0
    AuthService.add_membership(
        db,
        user.user_id,
        org.organization_id,
        role,
        is_default=is_default,
    )


def main() -> None:
    print(f"DATABASE_URL host target: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else '(local)'}")
    db = SessionLocal()
    try:
        create_platform_and_shared_tables(db)
        default_org = provision_default_tenant_if_missing(db)

        # 1) SaaS platform owner
        platform_user, platform_created = _ensure_user(
            db,
            username=PLATFORM["username"],
            email=PLATFORM["email"],
            password=PLATFORM["password"],
            full_name=PLATFORM["full_name"],
            is_platform_admin=True,
        )
        if default_org:
            _ensure_membership(db, platform_user, default_org, "ADMIN")

        # 2) Demo customer org + org admin (NOT platform admin)
        org1 = _ensure_org(db, ORG1["slug"], ORG1["name"], ORG1["plan"])
        org1_user, org1_created = _ensure_user(
            db,
            username=ORG1["username"],
            email=ORG1["email"],
            password=ORG1["password"],
            full_name=ORG1["full_name"],
            is_platform_admin=False,
        )
        _ensure_membership(db, org1_user, org1, "ADMIN")

        # 3) Second company so platform console shows multiple orgs
        org2 = _ensure_org(db, ORG2["slug"], ORG2["name"], ORG2["plan"])
        org2_user, org2_created = _ensure_user(
            db,
            username=ORG2["username"],
            email=ORG2["email"],
            password=ORG2["password"],
            full_name=ORG2["full_name"],
            is_platform_admin=False,
        )
        _ensure_membership(db, org2_user, org2, "ADMIN")

        orgs = db.query(Organization).order_by(Organization.organization_id).all()

        print()
        print("=" * 64)
        print(" DEMO ACCOUNTS SEEDED")
        print("=" * 64)
        print()
        print("[1] SaaS PLATFORM ADMIN (you see all companies)")
        print(f"  username: {PLATFORM['username']}")
        print(f"  password: {PLATFORM['password']}")
        print(f"  email:    {PLATFORM['email']}")
        print(f"  status:   {'created' if platform_created else 'updated'}")
        print("  after login -> user menu -> Platform console")
        print("                or open /platform/organizations")
        print()
        print("[2] COMPANY ORG ADMIN - Acme Metals (customer view)")
        print(f"  username: {ORG1['username']}")
        print(f"  password: {ORG1['password']}")
        print(f"  email:    {ORG1['email']}")
        print(f"  org:      {org1.name} ({org1.slug})")
        print(f"  status:   {'created' if org1_created else 'updated'}")
        print("  after login -> /dashboard (importer product)")
        print()
        print("[3] COMPANY ORG ADMIN - Karachi Copper (2nd customer)")
        print(f"  username: {ORG2['username']}")
        print(f"  password: {ORG2['password']}")
        print(f"  email:    {ORG2['email']}")
        print(f"  org:      {org2.name} ({org2.slug})")
        print(f"  status:   {'created' if org2_created else 'updated'}")
        print()
        print("[*] Organizations in platform console")
        for o in orgs:
            print(f"  - {o.name}  slug={o.slug}  status={o.status}  schema={o.schema_name}")
        print()
        print("Sign in at frontend /login")
        print("=" * 64)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nEnsure PostgreSQL is running and DATABASE_URL is set.")
        print("Default: postgresql://postgres:postgres@localhost:5432/lme_monitoring")
        sys.exit(1)
