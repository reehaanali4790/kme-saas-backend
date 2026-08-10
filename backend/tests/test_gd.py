"""Tests for the base Goods Declaration CRUD migration (modules/weboc/gd_router.py,
gd_service.py, gd_schemas.py) - distinct from tests/test_weboc.py, which covers the
GD-view/item-details/into-bond/ex-bond "extended workflow" cluster.
"""
from decimal import Decimal

from models.database_models import GoodsDeclaration, Shipment
from modules.weboc.gd_schemas import GDItemIn, GDSave, GDStatusUpdate


# ---------------------------------------------------------------------------
# Schema-level: lenient parsing preserved from the original _apply()/_dec()/_num()
# ---------------------------------------------------------------------------

def test_schema_tolerates_malformed_decimal():
    data = GDSave(gross_weight_mt="not-a-number")
    assert data.gross_weight_mt is None


def test_schema_tolerates_malformed_date():
    data = GDSave(filing_date="garbage")
    assert data.filing_date is None


def test_schema_tolerates_malformed_package_count():
    data = GDSave(package_count="abc")
    assert data.package_count is None


def test_schema_rejects_invalid_gd_type_silently():
    data = GDSave(gd_type="NOT_A_TYPE")
    assert data.gd_type is None


def test_schema_tolerates_non_list_levies():
    """Matches the original _clean_levies()'s isinstance(levies, list) guard."""
    data = GDSave(levy_breakdown="not-a-list")
    assert data.levy_breakdown is None


def test_item_schema_tolerates_malformed_decimal():
    """Matches the original _dec() exactly: Decimal(str(v)) with no comma-stripping, so
    a comma-formatted string is "malformed" here too (unlike bank_limits' _dec(), which
    is a different, comma-stripping helper - see utils/parsing.py's docstrings)."""
    item = GDItemIn(quantity="garbage", unit_value_declared="12,345.67")
    assert item.quantity is None
    assert item.unit_value_declared is None

    clean = GDItemIn(unit_value_declared="12345.67")
    assert clean.unit_value_declared == Decimal("12345.67")


def test_status_update_normalizes_case():
    assert GDStatusUpdate(status="filed").status == "FILED"


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def _make_shipment_and_gd(db_session, ref="SH-GD-TEST"):
    shipment = Shipment(shipment_ref=ref)
    db_session.add(shipment)
    db_session.commit()
    gd = GoodsDeclaration(shipment_id=shipment.shipment_id, gd_type="HOME_CONSUMPTION",
                          status="DRAFT", source="MANUAL")
    db_session.add(gd)
    db_session.commit()
    return shipment, gd


def test_viewer_can_read_but_not_write(client, make_user, db_session):
    _shipment, gd = _make_shipment_and_gd(db_session)
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get(f"/api/gd/{gd.gd_id}", headers=headers).status_code == 200

    resp = client.put(f"/api/gd/{gd.gd_id}", headers=headers, json={"gd_number": "SHOULD-FAIL"})
    assert resp.status_code == 403

    resp2 = client.post(f"/api/gd/{gd.gd_id}/advance", headers=headers)
    assert resp2.status_code == 403


def test_operator_create_get_update_delete_lifecycle(authenticated_client, db_session):
    shipment, _gd = _make_shipment_and_gd(db_session, ref="SH-GD-CREATE-TEST")

    create = authenticated_client.post("/api/gd/", json={
        "shipment_id": shipment.shipment_id,
        "gd_number": "KEWB-HC-001-21-07-2026",
        "gross_weight_mt": "12.5",
        "items": [{"hs_code": "7210.4910", "goods_description": "Steel Coil",
                   "quantity": "12.5", "unit_value_declared": "593.00 PER M/TON"}],
    })
    assert create.status_code == 200, create.text
    gd_id = create.json()["gd_id"]

    got = authenticated_client.get(f"/api/gd/{gd_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["gd_number"] == "KEWB-HC-001-21-07-2026"
    assert body["gross_weight_mt"] == 12.5
    assert len(body["items"]) == 1

    updated = authenticated_client.put(f"/api/gd/{gd_id}", json={"notes": "test note"})
    assert updated.status_code == 200

    got2 = authenticated_client.get(f"/api/gd/{gd_id}")
    assert got2.json()["notes"] == "test note"

    deleted = authenticated_client.delete(f"/api/gd/{gd_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/gd/{gd_id}")
    assert missing.status_code == 404


def test_decimal_field_explicit_clear_on_present_key(authenticated_client, db_session):
    """Regression guard for the DEC_FIELDS clear-on-present-key semantics: sending an
    empty string for a decimal field actively clears it (matches the original _apply())."""
    shipment, _gd = _make_shipment_and_gd(db_session, ref="SH-GD-CLEAR-TEST")
    create = authenticated_client.post("/api/gd/", json={
        "shipment_id": shipment.shipment_id, "gross_weight_mt": "50",
    })
    gd_id = create.json()["gd_id"]
    assert authenticated_client.get(f"/api/gd/{gd_id}").json()["gross_weight_mt"] == 50.0

    authenticated_client.put(f"/api/gd/{gd_id}", json={"gross_weight_mt": ""})
    assert authenticated_client.get(f"/api/gd/{gd_id}").json()["gross_weight_mt"] is None


def test_advance_status_workflow(authenticated_client, db_session):
    shipment, _gd = _make_shipment_and_gd(db_session, ref="SH-GD-ADVANCE-TEST")
    create = authenticated_client.post("/api/gd/", json={
        "shipment_id": shipment.shipment_id, "gd_type": "HOME_CONSUMPTION",
    })
    gd_id = create.json()["gd_id"]

    resp = authenticated_client.post(f"/api/gd/{gd_id}/advance")
    assert resp.status_code == 200
    assert resp.json()["status"] != "DRAFT"


def test_set_status_rejects_invalid_status(authenticated_client, db_session):
    shipment, _gd = _make_shipment_and_gd(db_session, ref="SH-GD-STATUS-TEST")
    create = authenticated_client.post("/api/gd/", json={"shipment_id": shipment.shipment_id})
    gd_id = create.json()["gd_id"]

    resp = authenticated_client.put(f"/api/gd/{gd_id}/status", json={"status": "NOT_A_STAGE"})
    assert resp.status_code == 400
