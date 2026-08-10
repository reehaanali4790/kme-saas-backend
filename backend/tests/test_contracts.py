"""Tests for the contracts module migration."""
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from modules.contracts.schemas import ContractSave, ContractStatusUpdate


# ---------------------------------------------------------------------------
# Schema-level: lenient date/status handling matching the original _apply()
# ---------------------------------------------------------------------------

def test_lenient_date_becomes_none_on_malformed_input():
    data = ContractSave(contract_date="not-a-date")
    assert data.contract_date is None


def test_lenient_status_ignores_invalid_value():
    data = ContractSave(status="NOT_A_REAL_STATUS")
    assert data.status is None


def test_lenient_status_accepts_valid_value():
    data = ContractSave(status="final")
    assert data.status == "FINAL"


def test_status_update_endpoint_rejects_invalid_status_strictly():
    """Unlike the general save/update schema, the dedicated status endpoint
    validates strictly (matches the original set_status behavior)."""
    with pytest.raises(PydanticValidationError):
        ContractStatusUpdate(status="NOT_A_REAL_STATUS")


def test_status_update_normalizes_case():
    assert ContractStatusUpdate(status="actual").status == "ACTUAL"


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/contracts/", headers=headers).status_code == 200

    resp = client.post("/api/contracts/", headers=headers, json={"supplier_name": "Test Co"})
    assert resp.status_code == 403


def test_operator_can_create_get_update_and_set_status(authenticated_client):
    create = authenticated_client.post("/api/contracts/", json={
        "supplier_name": "Acme Steel Supplier",
        "buyer_name": "Perfect Craft",
        "currency": "USD",
        "line_items": [
            {"product_name": "Steel Coil", "weight_mt": "25.5", "lc_amount": "10000"},
        ],
    })
    assert create.status_code == 200, create.text
    contract_id = create.json()["contract_id"]

    got = authenticated_client.get(f"/api/contracts/{contract_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["supplier_name"] == "Acme Steel Supplier"
    assert body["status"] == "DRAFT"
    assert len(body["line_items"]) == 1
    assert body["total_weight_mt"] == 25.5

    updated = authenticated_client.put(f"/api/contracts/{contract_id}", json={"notes": "test note"})
    assert updated.status_code == 200

    got2 = authenticated_client.get(f"/api/contracts/{contract_id}")
    assert got2.json()["notes"] == "test note"

    status_resp = authenticated_client.put(f"/api/contracts/{contract_id}/status", json={"status": "FINAL"})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "FINAL"

    bad_status = authenticated_client.put(f"/api/contracts/{contract_id}/status", json={"status": "BOGUS"})
    assert bad_status.status_code == 422
    assert isinstance(bad_status.json()["detail"], str)

    deleted = authenticated_client.delete(f"/api/contracts/{contract_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/contracts/{contract_id}")
    assert missing.status_code == 404
