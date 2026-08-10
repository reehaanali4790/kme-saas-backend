"""Tests for the Insurance module migration (modules/documents/insurance_router.py,
insurance_service.py, insurance_schemas.py)."""
from modules.documents.insurance_schemas import InsuranceSave
from modules.documents.insurance_service import verify_one, verify


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = InsuranceSave(sum_insured="garbage")
    assert data.sum_insured is None


def test_schema_tolerates_malformed_date():
    data = InsuranceSave(issue_date="garbage")
    assert data.issue_date is None


def test_decimal_present_key_clears_semantics():
    data = InsuranceSave(sum_insured=None)
    assert "sum_insured" in data.model_fields_set


# ---------------------------------------------------------------------------
# verify_one / verify — reference-number cross-check, moved to insurance_service.py
# (this is the function modules/shipments/services.py imports as verify_ref)
# ---------------------------------------------------------------------------

def test_verify_one_matched_case_and_punctuation_insensitive():
    assert verify_one("MSC-123/456", "msc123456") == "MATCHED"


def test_verify_one_not_matched():
    assert verify_one("ABC-1", "XYZ-2") == "NOT_MATCHED"


def test_verify_one_not_found_when_extracted_blank():
    assert verify_one(None, "XYZ-2") == "NOT_FOUND"


def test_verify_one_not_found_when_shipment_blank():
    assert verify_one("ABC-1", None) == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/insurance/by-shipment/999999", headers=headers).status_code == 200

    resp = client.post("/api/insurance/", headers=headers, json={"certificate_number": "X"})
    assert resp.status_code == 403


def test_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/insurance/", json={
        "certificate_number": "INS-TEST-001", "sum_insured": "50000.00",
        "gross_premium": "125.00", "issue_date": "2026-01-15",
    })
    assert create.status_code == 200, create.text
    insurance_id = create.json()["insurance_id"]

    got = authenticated_client.get(f"/api/insurance/{insurance_id}")
    assert got.status_code == 200
    assert got.json()["certificate_number"] == "INS-TEST-001"
    assert got.json()["sum_insured"] == 50000.0
    assert got.json()["premium_rate_pct"] == 0.25
    assert got.json()["status"] == "VERIFIED"

    updated = authenticated_client.put(f"/api/insurance/{insurance_id}", json={"notes": "test note"})
    assert updated.status_code == 200
    assert authenticated_client.get(f"/api/insurance/{insurance_id}").json()["notes"] == "test note"

    cleared = authenticated_client.put(f"/api/insurance/{insurance_id}", json={"sum_insured": None})
    assert cleared.status_code == 200
    assert authenticated_client.get(f"/api/insurance/{insurance_id}").json()["sum_insured"] is None

    deleted = authenticated_client.delete(f"/api/insurance/{insurance_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/insurance/{insurance_id}")
    assert missing.status_code == 404


def test_str_field_explicit_null_does_not_clear(authenticated_client):
    create = authenticated_client.post("/api/insurance/", json={"certificate_number": "INS-KEEP-001"})
    insurance_id = create.json()["insurance_id"]

    resp = authenticated_client.put(f"/api/insurance/{insurance_id}", json={"certificate_number": None})
    assert resp.status_code == 200
    assert authenticated_client.get(f"/api/insurance/{insurance_id}").json()["certificate_number"] == "INS-KEEP-001"
