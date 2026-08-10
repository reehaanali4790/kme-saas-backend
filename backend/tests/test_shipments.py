"""Tests for the Shipment hub module migration (modules/shipments/router.py,
services.py, schemas.py)."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError

from models.database_models import LCMaster
from modules.shipments.schemas import ShipmentCreate, ShipmentUpdate


# ---------------------------------------------------------------------------
# Schema-level: mixed field-update semantics + future-date validation
# ---------------------------------------------------------------------------

def test_future_event_date_rejected():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(PydanticValidationError):
        ShipmentUpdate(bl_date=tomorrow)


def test_past_event_date_accepted():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    data = ShipmentUpdate(bl_date=yesterday)
    assert data.bl_date == date.today() - timedelta(days=1)


def test_eta_and_maturity_date_are_not_future_blocked():
    """ETA and maturity_date are estimates/forward-dated by design - not in the
    future-blocked set (matches the original _FUTURE_BLOCKED dict)."""
    far_future = (date.today() + timedelta(days=365)).isoformat()
    data = ShipmentUpdate(eta=far_future, maturity_date=far_future)
    assert data.eta is not None
    assert data.maturity_date is not None


def test_free_text_field_present_key_clears():
    data = ShipmentUpdate(remarks=None)
    assert "remarks" in data.model_fields_set


def test_bl_date_only_applies_if_truthy_not_present_key():
    """bl_date/eta/etd use "only if truthy" semantics, unlike the milestone dates -
    this is asserted in apply_shipment_fields(), but the schema itself just needs to
    parse a malformed value leniently to None."""
    data = ShipmentUpdate(bl_date="garbage")
    assert data.bl_date is None


def test_lc_id_required_for_create():
    with pytest.raises(PydanticValidationError):
        ShipmentCreate()


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def _make_lc(db_session, lc_number="LC-SHIP-TEST"):
    lc = LCMaster(lc_number=lc_number, lc_date=date(2026, 1, 1),
                  monitoring_expiry=date(2026, 12, 31), status="OPEN")
    db_session.add(lc)
    db_session.commit()
    return lc


def test_viewer_can_list_but_not_create(client, make_user, db_session):
    lc = _make_lc(db_session)
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/shipments/", headers=headers).status_code == 200

    resp = client.post("/api/shipments/", headers=headers, json={"lc_id": lc.lc_id})
    assert resp.status_code == 403


def test_operator_create_get_update_delete_restore_lifecycle(authenticated_client, db_session):
    lc = _make_lc(db_session, lc_number="LC-SHIP-LIFECYCLE")

    create = authenticated_client.post("/api/shipments/", json={"lc_id": lc.lc_id})
    assert create.status_code == 200, create.text
    shipment_id = create.json()["shipment_id"]
    assert create.json()["category"] == "FIRST"

    got = authenticated_client.get(f"/api/shipments/{shipment_id}")
    assert got.status_code == 200
    assert got.json()["lc_id"] == lc.lc_id
    assert got.json()["status"] == "PENDING"

    updated = authenticated_client.put(f"/api/shipments/{shipment_id}", json={
        "vessel_name": "MSC TEST VESSEL", "remarks": "test remarks",
    })
    assert updated.status_code == 200

    got2 = authenticated_client.get(f"/api/shipments/{shipment_id}")
    assert got2.json()["vessel_name"] == "MSC TEST VESSEL"
    assert got2.json()["remarks"] == "test remarks"

    # setting a milestone date auto-derives status (fully automatic, not user-settable)
    updated2 = authenticated_client.put(f"/api/shipments/{shipment_id}", json={
        "intimation_date": date.today().isoformat(),
    })
    assert updated2.status_code == 200
    assert authenticated_client.get(f"/api/shipments/{shipment_id}").json()["status"] == "DOCS_AT_BANK"

    deleted = authenticated_client.delete(f"/api/shipments/{shipment_id}")
    assert deleted.status_code == 200
    assert deleted.json().get("already_deleted") is not True

    # soft-deleted shipments don't appear in the default list
    listing = authenticated_client.get("/api/shipments/", params={"lc_id": lc.lc_id})
    assert all(item["shipment_id"] != shipment_id for item in listing.json()["items"])

    # deleting again is idempotent
    deleted_again = authenticated_client.delete(f"/api/shipments/{shipment_id}")
    assert deleted_again.json()["already_deleted"] is True

    restored = authenticated_client.post(f"/api/shipments/{shipment_id}/restore")
    assert restored.status_code == 200
    listing2 = authenticated_client.get("/api/shipments/", params={"lc_id": lc.lc_id})
    assert any(item["shipment_id"] == shipment_id for item in listing2.json()["items"])


def test_create_shipment_future_date_returns_clean_string_detail(authenticated_client, db_session):
    lc = _make_lc(db_session, lc_number="LC-SHIP-FUTURE-TEST")
    create = authenticated_client.post("/api/shipments/", json={"lc_id": lc.lc_id})
    shipment_id = create.json()["shipment_id"]

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    resp = authenticated_client.put(f"/api/shipments/{shipment_id}", json={"bl_date": tomorrow})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_lc_balance_and_second_shipment_category(authenticated_client, db_session):
    lc = _make_lc(db_session, lc_number="LC-SHIP-BALANCE-TEST")
    first = authenticated_client.post("/api/shipments/", json={"lc_id": lc.lc_id})
    assert first.json()["category"] == "FIRST"
    second = authenticated_client.post("/api/shipments/", json={"lc_id": lc.lc_id})
    assert second.json()["category"] == "SECOND"

    balance = authenticated_client.get(f"/api/shipments/lc-balance/{lc.lc_id}")
    assert balance.status_code == 200
    assert "lc_remaining_mt" in balance.json()


def test_is_container_bl_package_types():
    from models.database_models import BillOfLading
    from modules.shipments.container_detention_service import is_container_bl

    bl_metal = BillOfLading(package_type="Metal", goods_description="Metal sheets 20FT Container")
    assert is_container_bl(bl_metal) is True

    bl_coils = BillOfLading(package_type="Steel Coils", goods_description="5 Coils of Hot Rolled Steel")
    assert is_container_bl(bl_coils) is False

    bl_coils_container = BillOfLading(package_type="Steel Coils", goods_description="5 Coils in 1x20FT FCL Container")
    assert is_container_bl(bl_coils_container) is True


def test_serialize_bl_demurrage_detention_scoping():
    from models.database_models import BillOfLading
    from modules.shipments.bl_service import bl_to_dict

    bl_coil = BillOfLading(bl_id=1, bl_type="COIL", package_type="Steel Coils", goods_description="Coils")
    coil_data = bl_to_dict(bl_coil)
    assert coil_data["bl_type"] == "COIL"
    assert coil_data["is_container_bl"] is False
    assert coil_data["detention"] == {}
    assert coil_data["demurrage"].get("state") == "UNKNOWN"

    bl_cntr = BillOfLading(bl_id=2, bl_type="CONTAINER", package_type="20FT Container", goods_description="Metal")
    cntr_data = bl_to_dict(bl_cntr)
    assert cntr_data["bl_type"] == "CONTAINER"
    assert cntr_data["is_container_bl"] is True
    assert cntr_data["demurrage"] == {}
    assert cntr_data["detention"].get("state") == "UNKNOWN"


def test_resolve_bl_type_prefers_ai_signal_then_falls_back():
    from models.database_models import BillOfLading
    from modules.shipments.container_detention_service import resolve_bl_type

    # AI extractor gave a confident answer — trust it even if the cargo fields
    # would otherwise heuristically suggest the opposite.
    bl = BillOfLading(package_type="Steel Coils", goods_description="5 Coils")
    assert resolve_bl_type({"bl_type": "CONTAINER"}, bl) == "CONTAINER"

    # AI extractor returned null/unset — fall back to the regex heuristic.
    bl_coil = BillOfLading(package_type="Steel Coils", goods_description="5 Coils of Hot Rolled Steel")
    assert resolve_bl_type({"bl_type": None}, bl_coil) == "COIL"
    assert resolve_bl_type(None, bl_coil) == "COIL"

    bl_cntr = BillOfLading(package_type="Metal", goods_description="Metal sheets 20FT Container")
    assert resolve_bl_type({}, bl_cntr) == "CONTAINER"

    # Garbage AI value is ignored, not trusted.
    bl_garbage = BillOfLading(package_type="Metal", goods_description="20FT Container")
    assert resolve_bl_type({"bl_type": "UNKNOWN"}, bl_garbage) == "CONTAINER"

