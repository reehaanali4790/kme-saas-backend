"""Tests for the Bill of Lading module migration (modules/shipments/bl_router.py,
bl_service.py, bl_schemas.py)."""
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from models.database_models import LCMaster
from modules.shipments.bl_schemas import BLSave, BLStatusUpdate


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = BLSave(gross_weight_mt="garbage")
    assert data.gross_weight_mt is None


def test_schema_tolerates_malformed_date():
    data = BLSave(bl_date="garbage")
    assert data.bl_date is None


def test_status_update_rejects_invalid_status():
    with pytest.raises(PydanticValidationError):
        BLStatusUpdate(status="BOGUS")


def test_status_update_normalizes_case():
    assert BLStatusUpdate(status="verified").status == "VERIFIED"


def test_present_key_clears_semantics():
    data = BLSave(bl_number=None)
    assert "bl_number" in data.model_fields_set


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/bl/", headers=headers).status_code == 200

    resp = client.post("/api/bl/", headers=headers, json={"bl_number": "MSC-TEST-001"})
    assert resp.status_code == 403


def test_operator_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/bl/", json={
        "bl_number": "MSC-BL-TEST-001", "vessel_name": "MSC TEST VESSEL",
        "gross_weight_mt": "25.5", "package_count": "10",
    })
    assert create.status_code == 200, create.text
    bl_id = create.json()["bl_id"]
    assert create.json()["bl_number"] == "MSC-BL-TEST-001"

    got = authenticated_client.get(f"/api/bl/{bl_id}")
    assert got.status_code == 200
    assert got.json()["vessel_name"] == "MSC TEST VESSEL"
    assert got.json()["gross_weight_mt"] == 25.5
    # a fresh manual BL not already PENDING_REVIEW is auto-verified
    assert got.json()["status"] == "VERIFIED"

    updated = authenticated_client.put(f"/api/bl/{bl_id}", json={"notes": "test note"})
    assert updated.status_code == 200
    assert authenticated_client.get(f"/api/bl/{bl_id}").json()["notes"] == "test note"

    status_resp = authenticated_client.put(f"/api/bl/{bl_id}/status", json={"status": "RELEASED"})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "RELEASED"

    deleted = authenticated_client.delete(f"/api/bl/{bl_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/bl/{bl_id}")
    assert missing.status_code == 404


def test_duplicate_bl_number_rejected(authenticated_client):
    first = authenticated_client.post("/api/bl/", json={"bl_number": "MSC-DUP-TEST"})
    assert first.status_code == 200

    second = authenticated_client.post("/api/bl/", json={"bl_number": "MSC-DUP-TEST"})
    assert second.status_code == 409


def test_link_bl_to_lc(authenticated_client, db_session):
    lc = LCMaster(lc_number="LC-BL-LINK-TEST", lc_date="2026-01-01",
                  monitoring_expiry="2026-12-31", status="OPEN")
    db_session.add(lc)
    db_session.commit()

    create = authenticated_client.post("/api/bl/", json={"bl_number": "MSC-LINK-TEST"})
    bl_id = create.json()["bl_id"]

    linked = authenticated_client.put(f"/api/bl/{bl_id}/link-lc", json={"lc_id": lc.lc_id})
    assert linked.status_code == 200
    assert linked.json()["lc_number"] == "LC-BL-LINK-TEST"

    got = authenticated_client.get(f"/api/bl/{bl_id}")
    assert got.json()["lc_id"] == lc.lc_id


def test_create_bl_missing_status_field_returns_clean_string_detail(authenticated_client):
    create = authenticated_client.post("/api/bl/", json={"bl_number": "MSC-ERR-TEST"})
    bl_id = create.json()["bl_id"]
    resp = authenticated_client.put(f"/api/bl/{bl_id}/status", json={"status": "NOT_REAL"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
