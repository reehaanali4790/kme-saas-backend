"""Tests for the Packing List module migration (modules/documents/packing_router.py,
packing_service.py, packing_schemas.py)."""
from modules.documents.packing_schemas import PackingSave


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = PackingSave(total_net_weight_mt="garbage")
    assert data.total_net_weight_mt is None


def test_schema_tolerates_malformed_date():
    data = PackingSave(packing_date="garbage")
    assert data.packing_date is None


def test_decimal_present_key_clears_semantics():
    data = PackingSave(total_net_weight_mt=None)
    assert "total_net_weight_mt" in data.model_fields_set


def test_line_items_tolerate_malformed_decimal():
    data = PackingSave(line_items=[{"item_number": "1", "quantity_mt": "garbage", "grade": "  A  "}])
    assert data.line_items[0].item_number == 1
    assert data.line_items[0].quantity_mt is None
    assert data.line_items[0].grade == "A"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/packing/", headers=headers, json={"packing_number": "X"})
    assert resp.status_code == 403


def test_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/packing/", json={
        "packing_number": "PKG-TEST-001", "total_net_weight_mt": "20.5",
        "packing_date": "2026-01-15",
        "line_items": [
            {"item_number": 1, "quantity_mt": "10.0", "grade": "A"},
            {"item_number": 2, "quantity_mt": "10.5", "grade": "B"},
        ],
    })
    assert create.status_code == 200, create.text
    packing_id = create.json()["packing_id"]

    got = authenticated_client.get(f"/api/packing/{packing_id}")
    assert got.status_code == 200
    assert got.json()["packing_number"] == "PKG-TEST-001"
    assert got.json()["total_net_weight_mt"] == 20.5
    assert len(got.json()["line_items"]) == 2
    assert got.json()["status"] == "VERIFIED"

    updated = authenticated_client.put(f"/api/packing/{packing_id}", json={"hs_code": "7225.1900"})
    assert updated.status_code == 200
    reget = authenticated_client.get(f"/api/packing/{packing_id}")
    assert reget.json()["hs_code"] == "7225.1900"
    # line_items absent from the PUT payload -> untouched (still 2)
    assert len(reget.json()["line_items"]) == 2

    replaced = authenticated_client.put(f"/api/packing/{packing_id}", json={
        "line_items": [{"item_number": 1, "quantity_mt": "9.0"}],
    })
    assert replaced.status_code == 200
    assert len(authenticated_client.get(f"/api/packing/{packing_id}").json()["line_items"]) == 1

    deleted = authenticated_client.delete(f"/api/packing/{packing_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/packing/{packing_id}")
    assert missing.status_code == 404


def test_str_field_explicit_null_does_not_clear(authenticated_client):
    create = authenticated_client.post("/api/packing/", json={"packing_number": "PKG-KEEP-001"})
    packing_id = create.json()["packing_id"]

    resp = authenticated_client.put(f"/api/packing/{packing_id}", json={"packing_number": None})
    assert resp.status_code == 200
    assert authenticated_client.get(f"/api/packing/{packing_id}").json()["packing_number"] == "PKG-KEEP-001"


def test_save_packing_marks_pending_shipment_as_documents_received(authenticated_client, db_session):
    from models.database_models import Shipment
    shipment = Shipment(shipment_ref="PKG-SHIP-TEST", status="PENDING")
    db_session.add(shipment)
    db_session.commit()

    create = authenticated_client.post("/api/packing/", json={
        "shipment_id": shipment.shipment_id, "packing_number": "PKG-STATUS-TEST",
    })
    assert create.status_code == 200

    db_session.refresh(shipment)
    assert shipment.status == "DOCUMENTS_RECEIVED"
