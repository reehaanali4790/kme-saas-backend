"""Tests for the Financial Instrument module migration (modules/documents/fi_router.py,
fi_service.py, fi_schemas.py)."""
from modules.documents.fi_schemas import FISave


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = FISave(fi_value="garbage")
    assert data.fi_value is None


def test_schema_tolerates_malformed_date():
    data = FISave(expiry_date="garbage")
    assert data.expiry_date is None


def test_schema_accepts_iso_datetime_string():
    data = FISave(expiry_date="2026-07-20T10:00:00")
    assert str(data.expiry_date) == "2026-07-20"


def test_decimal_present_key_clears_semantics():
    data = FISave(fi_value=None)
    assert "fi_value" in data.model_fields_set


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/fi/by-shipment/999999", headers=headers).status_code == 200

    resp = client.post("/api/fi/", headers=headers, json={"fi_number": "X"})
    assert resp.status_code == 403


def test_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/fi/", json={
        "fi_number": "FI-TEST-001", "fi_value": "1000.50", "exchange_rate": "280.25",
        "expiry_date": "2026-12-31",
    })
    assert create.status_code == 200, create.text
    fi_id = create.json()["fi_id"]

    got = authenticated_client.get(f"/api/fi/{fi_id}")
    assert got.status_code == 200
    assert got.json()["fi_number"] == "FI-TEST-001"
    assert got.json()["fi_value"] == 1000.5
    assert got.json()["expiry_date"] == "2026-12-31"
    # a manually-saved FI not already PENDING_REVIEW is auto-verified
    assert got.json()["status"] == "VERIFIED"

    updated = authenticated_client.put(f"/api/fi/{fi_id}", json={"notes": "test note"})
    assert updated.status_code == 200
    assert authenticated_client.get(f"/api/fi/{fi_id}").json()["notes"] == "test note"

    # explicit-null on a decimal field clears it (present-key-clears semantics)
    cleared = authenticated_client.put(f"/api/fi/{fi_id}", json={"fi_value": None})
    assert cleared.status_code == 200
    assert authenticated_client.get(f"/api/fi/{fi_id}").json()["fi_value"] is None

    deleted = authenticated_client.delete(f"/api/fi/{fi_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/fi/{fi_id}")
    assert missing.status_code == 404


def test_str_field_explicit_null_does_not_clear(authenticated_client):
    create = authenticated_client.post("/api/fi/", json={"fi_number": "FI-KEEP-001"})
    fi_id = create.json()["fi_id"]

    resp = authenticated_client.put(f"/api/fi/{fi_id}", json={"fi_number": None})
    assert resp.status_code == 200
    assert authenticated_client.get(f"/api/fi/{fi_id}").json()["fi_number"] == "FI-KEEP-001"


def test_fi_for_shipment_empty_list(authenticated_client):
    resp = authenticated_client.get("/api/fi/by-shipment/999999")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
