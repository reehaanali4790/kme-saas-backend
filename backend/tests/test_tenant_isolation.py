"""Multi-tenant isolation integration tests."""

import pytest
from config.database import SessionLocal, set_platform_search_path, set_tenant_search_path
from modules.tenants.provision import provision_tenant
from modules.auth.services import AuthService
from models.database_models import LCMaster


@pytest.fixture(scope="session")
def two_tenants(db_engine):
    db = SessionLocal()
    try:
        set_platform_search_path(db)
        a = provision_tenant(db, slug="tenant-a", name="Tenant A", plan_slug="operations")
        b = provision_tenant(db, slug="tenant-b", name="Tenant B", plan_slug="operations")
        admin_a = AuthService.create_user(db, "admin_a", "admin_a@test.local", "TestPass123!", "Admin A")
        admin_b = AuthService.create_user(db, "admin_b", "admin_b@test.local", "TestPass123!", "Admin B")
        AuthService.add_membership(db, admin_a.user_id, a.organization_id, "ADMIN", is_default=True)
        AuthService.add_membership(db, admin_b.user_id, b.organization_id, "ADMIN", is_default=True)
        return a, b, admin_a, admin_b
    finally:
        db.close()


def test_tenant_lc_isolation(db_engine, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        set_tenant_search_path(db_a, org_a.schema_name)
        lc_a = LCMaster(lc_number="LC-A-001", lc_date=__import__("datetime").date.today(),
                        monitoring_expiry=__import__("datetime").date.today())
        db_a.add(lc_a)
        db_a.commit()

        set_tenant_search_path(db_b, org_b.schema_name)
        found = db_b.query(LCMaster).filter(LCMaster.lc_number == "LC-A-001").first()
        assert found is None

        lc_b = LCMaster(lc_number="LC-B-001", lc_date=__import__("datetime").date.today(),
                        monitoring_expiry=__import__("datetime").date.today())
        db_b.add(lc_b)
        db_b.commit()

        set_tenant_search_path(db_a, org_a.schema_name)
        assert db_a.query(LCMaster).filter(LCMaster.lc_number == "LC-B-001").first() is None
    finally:
        db_a.close()
        db_b.close()


def test_cross_tenant_api_idor(client, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants

    login_a = client.post("/api/auth/login", data={"username": "admin_a", "password": "TestPass123!"})
    assert login_a.status_code == 200
    token_a = login_a.json()["access_token"]

    login_b = client.post("/api/auth/login", data={"username": "admin_b", "password": "TestPass123!"})
    token_b = login_b.json()["access_token"]

    # Tenant A creates LC via API would need lc-table endpoint - smoke test auth tokens differ
    assert token_a != token_b
