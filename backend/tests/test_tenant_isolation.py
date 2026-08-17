"""Multi-tenant isolation integration tests."""

import pytest
from datetime import date
from sqlalchemy.orm import Session

from config.database import SessionLocal, set_platform_search_path, set_tenant_search_path
from models.database_models import Contract, LCMaster, Shipment
from modules.tenants.provision import provision_tenant
from modules.auth.services import AuthService
from starlette.testclient import TestClient


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
        db.commit()
        return a, b, admin_a, admin_b
    finally:
        db.close()


@pytest.fixture()
def mt_client(db_engine):
    """TestClient with real tenant DB routing (no default-tenant override)."""
    from main import app

    app.state.limiter.reset()
    with TestClient(app) as test_client:
        yield test_client


def _auth_headers(client: TestClient, username: str, password: str = "TestPass123!") -> dict:
    login = client.post("/api/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _seed_shipment_in_tenant(schema_name: str) -> int:
    db = SessionLocal()
    try:
        set_tenant_search_path(db, schema_name)
        contract = Contract(contract_number=f"C-{schema_name}", status="FINAL", source="MANUAL")
        db.add(contract)
        db.flush()
        lc = LCMaster(
            lc_number=f"LC-{schema_name}",
            lc_date=date.today(),
            monitoring_expiry=date.today(),
            contract_id=contract.contract_id,
        )
        db.add(lc)
        db.flush()
        shipment = Shipment(
            contract_id=contract.contract_id,
            lc_id=lc.lc_id,
            shipment_ref=f"REF-{schema_name}",
        )
        db.add(shipment)
        db.commit()
        return shipment.shipment_id
    finally:
        db.close()


def test_tenant_lc_isolation(db_engine, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        set_tenant_search_path(db_a, org_a.schema_name)
        lc_a = LCMaster(
            lc_number="LC-A-001",
            lc_date=date.today(),
            monitoring_expiry=date.today(),
        )
        db_a.add(lc_a)
        db_a.commit()

        set_tenant_search_path(db_b, org_b.schema_name)
        found = db_b.query(LCMaster).filter(LCMaster.lc_number == "LC-A-001").first()
        assert found is None

        lc_b = LCMaster(
            lc_number="LC-B-001",
            lc_date=date.today(),
            monitoring_expiry=date.today(),
        )
        db_b.add(lc_b)
        db_b.commit()

        set_tenant_search_path(db_a, org_a.schema_name)
        assert db_a.query(LCMaster).filter(LCMaster.lc_number == "LC-B-001").first() is None
    finally:
        db_a.close()
        db_b.close()


def test_cross_tenant_shipment_api_idor(mt_client, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants
    shipment_id = _seed_shipment_in_tenant(org_a.schema_name)

    headers_a = _auth_headers(mt_client, "admin_a")
    headers_b = _auth_headers(mt_client, "admin_b")

    own = mt_client.get(f"/api/shipments/{shipment_id}", headers=headers_a)
    assert own.status_code == 200
    assert own.json()["shipment_id"] == shipment_id

    cross = mt_client.get(f"/api/shipments/{shipment_id}", headers=headers_b)
    assert cross.status_code == 404


def test_cross_tenant_lc_list_isolated(mt_client, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants

    db = SessionLocal()
    try:
        set_tenant_search_path(db, org_a.schema_name)
        db.add(LCMaster(lc_number="LC-LIST-A", lc_date=date.today(), monitoring_expiry=date.today()))
        db.commit()
    finally:
        db.close()

    headers_b = _auth_headers(mt_client, "admin_b")
    resp = mt_client.get("/api/lc-table/list", headers=headers_b)
    assert resp.status_code == 200
    payload = resp.json()
    rows = payload.get("items") or payload.get("data") or []
    numbers = [row.get("lc_number") for row in rows]
    assert "LC-LIST-A" not in numbers


def test_cross_tenant_wrong_org_select_forbidden(mt_client, two_tenants):
    org_a, org_b, admin_a, admin_b = two_tenants
    login = mt_client.post("/api/auth/login", data={"username": "admin_a", "password": "TestPass123!"})
    token = login.json()["access_token"]

    select_b = mt_client.post(
        "/api/auth/select-org",
        headers={"Authorization": f"Bearer {token}"},
        json={"org_id": org_b.organization_id},
    )
    assert select_b.status_code == 403
