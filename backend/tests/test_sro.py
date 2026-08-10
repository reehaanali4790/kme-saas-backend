"""Tests for the SRO / EDB Quota module migration (modules/weboc/sro_router.py,
sro_service.py, sro_schemas.py)."""
from decimal import Decimal

from models.database_models import EdbApproval, GoodsDeclaration, Shipment, SroQuotaItem
from modules.weboc.sro_schemas import ApprovalSave, SroQuotaItemSave


# ---------------------------------------------------------------------------
# Schema-level: uniform "present key applies, even to clear" semantics
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_date():
    data = ApprovalSave(start_date="garbage")
    assert data.start_date is None


def test_schema_tolerates_malformed_decimal():
    data = ApprovalSave(approved_qty_mt="not-a-number")
    assert data.approved_qty_mt is None


def test_schema_tolerates_non_list_group_numbers():
    data = ApprovalSave(group_sro_numbers="not-a-list")
    assert data.group_sro_numbers is None


def test_present_key_semantics_via_model_fields_set():
    """The key distinguishing feature of this module's update convention: presence of a
    key (even with a null-ish value) is what triggers apply/clear - not truthiness."""
    data = ApprovalSave(company_name=None)
    assert "company_name" in data.model_fields_set
    untouched = ApprovalSave(main_sro_no="X")
    assert "company_name" not in untouched.model_fields_set


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/sro/approvals", headers=headers).status_code == 200

    resp = client.post("/api/sro/approvals", headers=headers, json={
        "main_sro_no": "7210.4910", "company_name": "Test Co", "approved_qty_mt": 100,
    })
    assert resp.status_code == 403


def test_operator_create_get_update_delete_approval(authenticated_client):
    create = authenticated_client.post("/api/sro/approvals", json={
        "main_sro_no": "7210.4910", "company_name": "Perfect Craft",
        "approved_qty_mt": "1000", "hs_code": "7210.4910",
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "group_sro_numbers": [{"group_sro_no": "7210.3010"}],
    })
    assert create.status_code == 200, create.text
    approval_id = create.json()["approval_id"]
    assert create.json()["approved_qty_mt"] == 1000.0

    got = authenticated_client.get(f"/api/sro/approvals/{approval_id}")
    assert got.status_code == 200
    assert got.json()["company_name"] == "Perfect Craft"
    assert len(got.json()["group_sro_numbers"]) == 1

    updated = authenticated_client.put(f"/api/sro/approvals/{approval_id}",
                                        json={"notes": "test note"})
    assert updated.status_code == 200
    assert updated.json()["notes"] == "test note"
    # unrelated fields survive a partial update
    assert updated.json()["company_name"] == "Perfect Craft"

    deleted = authenticated_client.delete(f"/api/sro/approvals/{approval_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/sro/approvals/{approval_id}")
    assert missing.status_code == 404


def test_create_approval_missing_required_field_returns_clean_string_detail(authenticated_client):
    resp = authenticated_client.post("/api/sro/approvals", json={"approved_qty_mt": 100})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_delete_approval_blocked_when_in_use(authenticated_client, db_session):
    create = authenticated_client.post("/api/sro/approvals", json={
        "main_sro_no": "7210.9999", "company_name": "In Use Co", "approved_qty_mt": 500,
    })
    approval_id = create.json()["approval_id"]

    shipment = Shipment(shipment_ref="SH-SRO-TEST")
    db_session.add(shipment)
    db_session.commit()
    gd = GoodsDeclaration(shipment_id=shipment.shipment_id, gd_type="HOME_CONSUMPTION")
    db_session.add(gd)
    db_session.commit()

    item_resp = authenticated_client.post(f"/api/sro/gd/{gd.gd_id}/items", json={
        "approval_id": approval_id, "declared_qty_mt": "50",
    })
    assert item_resp.status_code == 200, item_resp.text

    deleted = authenticated_client.delete(f"/api/sro/approvals/{approval_id}")
    assert deleted.status_code == 409


def test_gd_item_update_clears_approval_id_on_explicit_null(authenticated_client, db_session):
    create = authenticated_client.post("/api/sro/approvals", json={
        "main_sro_no": "7210.1111", "company_name": "Clear Test Co", "approved_qty_mt": 200,
    })
    approval_id = create.json()["approval_id"]

    shipment = Shipment(shipment_ref="SH-SRO-CLEAR-TEST")
    db_session.add(shipment)
    db_session.commit()
    gd = GoodsDeclaration(shipment_id=shipment.shipment_id, gd_type="HOME_CONSUMPTION")
    db_session.add(gd)
    db_session.commit()

    item_resp = authenticated_client.post(f"/api/sro/gd/{gd.gd_id}/items", json={
        "approval_id": approval_id, "declared_qty_mt": "10",
    })
    item_id = item_resp.json()["item_id"]
    assert item_resp.json()["approval_id"] == approval_id

    cleared = authenticated_client.put(f"/api/sro/items/{item_id}", json={"approval_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["approval_id"] is None
