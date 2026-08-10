"""Tests for the WeBOC GD module migration.
"""
from decimal import Decimal
from datetime import date, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from models.database_models import GoodsDeclaration, Shipment, GdKgtlWeighment, ExBondEntry, GDItem
from modules.weboc.schemas import GDViewSave, GdKgtlWeighmentSave, BondPenaltySave


# ---------------------------------------------------------------------------
# Unit tests: Lenient validation in schemas
# ---------------------------------------------------------------------------
def test_schema_tolerates_malformed_dates():
    data = GDViewSave(gd_id=1, filing_date="garbage-date-extracted")
    assert data.filing_date is None


def test_schema_tolerates_malformed_decimals():
    data = GDViewSave(gd_id=1, gross_weight_mt="invalid-float")
    assert data.gross_weight_mt is None


def test_schema_tolerates_malformed_ints():
    data = GDViewSave(gd_id=1, package_count="not-an-int")
    assert data.package_count is None


def test_penalty_schema_validation():
    # manual penalty requires a reason
    with pytest.raises(PydanticValidationError):
        BondPenaltySave(penalty_pkr=1000, source="MANUAL", reason="   ")

    # document source does not require a reason
    penalty = BondPenaltySave(penalty_pkr=1000, source="DOCUMENT")
    assert penalty.penalty_pkr == Decimal("1000")


# ---------------------------------------------------------------------------
# Integration tests: HTTP Endpoints & Permission Gate
# ---------------------------------------------------------------------------
def test_viewer_cannot_mutate(client, make_user, db_session):
    # Insert mock shipment
    shipment = Shipment(shipment_ref="SH-VIEWER-TEST")
    db_session.add(shipment)
    db_session.commit()

    gd = GoodsDeclaration(shipment_id=shipment.shipment_id, gd_type="HOME_CONSUMPTION")
    db_session.add(gd)
    db_session.commit()

    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Try save GD View
    resp = client.post("/api/gd-view/", headers=headers, json={"gd_id": gd.gd_id, "gd_number": "TEST"})
    assert resp.status_code == 403

    # Try save penalty
    resp = client.put(f"/api/weboc/{gd.gd_id}/bond-penalty", headers=headers, json={"penalty_pkr": 100, "source": "DOCUMENT"})
    assert resp.status_code == 403


def test_operator_crud_lifecycle(authenticated_client, db_session):
    # Insert mock shipment
    shipment = Shipment(shipment_ref="SH-OPERATOR-TEST")
    db_session.add(shipment)
    db_session.commit()

    # The shipment's first upload usually creates the GD, let's simulate it by inserting one
    gd = GoodsDeclaration(shipment_id=shipment.shipment_id, gd_type="HOME_CONSUMPTION", status="DRAFT")
    db_session.add(gd)
    db_session.commit()

    # 1. Save GD View manually
    resp = authenticated_client.post("/api/gd-view/", json={
        "gd_id": gd.gd_id,
        "gd_number": "KEWB-HC-999-20-07-2026",
        "filing_date": "2026-07-20",
        "gross_weight_mt": "50.5",
        "total_duties_pkr": "250000",
        "customs_duty_pkr": "100000",
        "sales_tax_pkr": "150000",
        "declaration_type": "Home Consumption",
        "gd_type": "HOME_CONSUMPTION"
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    # Check GD is updated
    db_session.refresh(gd)
    assert gd.gd_number == "KEWB-HC-999-20-07-2026"
    assert gd.filing_date == date(2026, 7, 20)
    assert gd.gross_weight_mt == Decimal("50.5")

    # 2. Save Item Details
    resp = authenticated_client.post("/api/item-details/", json={
        "gd_id": gd.gd_id,
        "items": [
            {
                "item_number": 1,
                "hs_code": "7210.4910",
                "goods_description": "Galvanized Steel Coils",
                "quantity": 50.5,
                "unit": "MT"
            }
        ]
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    # Check items exist
    db_session.refresh(gd)
    assert len(gd.items) == 1
    assert gd.items[0].hs_code == "7210.4910"
    assert gd.items[0].quantity == Decimal("50.5")

    # 3. Get WeBOC Summary
    resp = authenticated_client.get(f"/api/weboc/{gd.gd_id}/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["gd_view"]["gd_number"] == "KEWB-HC-999-20-07-2026"
    assert len(summary["item_details"]["items"]) == 1

    # 4. Override Declaration Type
    resp = authenticated_client.put(f"/api/weboc/{gd.gd_id}/declaration-type", json={
        "gd_type": "INTO_BOND",
        "bonded_qty_mt": 50.5
    })
    assert resp.status_code == 200
    db_session.refresh(gd)
    assert gd.gd_type == "INTO_BOND"

    # 4b. An Ex-Bond entry cannot be recorded yet - flagging the GD Into-Bond via the
    # manual override does not mean an actual Into-Bond GD has been filed. This is the
    # "EB can never exist without an IB" rule, enforced consistently on both the
    # document-upload and manual-entry paths.
    resp = authenticated_client.post(f"/api/weboc/{gd.gd_id}/ex-bond", json={
        "gd_number": "KEWB-XB-111",
        "ex_bond_date": "2026-07-21",
        "quantity_mt": 10.0,
        "duties_paid_pkr": 50000,
        "force": False
    })
    assert resp.status_code == 422, resp.text

    # File the Into-Bond GD itself before Ex-Bond entries are allowed.
    gd.into_bond_gd_uploaded = True
    db_session.commit()

    # 5. Record Ex-Bond entry (lifting)
    # The default tolerance will check if we over-lift.
    resp = authenticated_client.post(f"/api/weboc/{gd.gd_id}/ex-bond", json={
        "gd_number": "KEWB-XB-111",
        "ex_bond_date": "2026-07-21",
        "quantity_mt": 10.0,
        "duties_paid_pkr": 50000,
        "force": False
    })
    assert resp.status_code == 200, resp.text
    entry_id = resp.json()["entry_id"]

    eb = db_session.query(ExBondEntry).filter(ExBondEntry.entry_id == entry_id).first()
    assert eb is not None
    assert eb.quantity_mt == Decimal("10.0")

    # 6. Update Ex-Bond entry
    resp = authenticated_client.put(f"/api/weboc/ex-bond/{entry_id}", json={
        "quantity_mt": 15.0,
        "remarks": "updated lifting"
    })
    assert resp.status_code == 200
    db_session.refresh(eb)
    assert eb.quantity_mt == Decimal("15.0")
    assert eb.remarks == "updated lifting"

    # 7. Penalty recording (force allows it since it might not be in breach yet)
    resp = authenticated_client.put(f"/api/weboc/{gd.gd_id}/bond-penalty", json={
        "penalty_pkr": 5000,
        "source": "MANUAL",
        "reason": "Force record penalty for testing",
        "force": True
    })
    assert resp.status_code == 200, resp.text
    db_session.refresh(gd)
    assert gd.bond_penalty_pkr == Decimal("5000")

    # Clear penalty
    resp = authenticated_client.delete(f"/api/weboc/{gd.gd_id}/bond-penalty")
    assert resp.status_code == 200
    db_session.refresh(gd)
    assert gd.bond_penalty_pkr is None

    # 8. Record vehicle trip weighments (KGTL)
    resp = authenticated_client.post(f"/api/weboc/{gd.gd_id}/kgtl", json={
        "vehicle_number": "TRK-999",
        "weigh_date": "2026-07-20",
        "own_weight_kg": 25000,
        "kgtl_weight_kg": 25020,
        "remarks": "Test weighment"
    })
    assert resp.status_code == 200, resp.text
    weighment_id = resp.json()["weighment_id"]

    w = db_session.query(GdKgtlWeighment).filter(GdKgtlWeighment.weighment_id == weighment_id).first()
    assert w is not None
    assert w.own_weight_kg == Decimal("25000")

    # Update weighment
    resp = authenticated_client.put(f"/api/weboc/kgtl/{weighment_id}", json={
        "own_weight_kg": 25010
    })
    assert resp.status_code == 200
    db_session.refresh(w)
    assert w.own_weight_kg == Decimal("25010")

    # Delete weighment
    resp = authenticated_client.delete(f"/api/weboc/kgtl/{weighment_id}")
    assert resp.status_code == 200
    assert db_session.query(GdKgtlWeighment).filter(GdKgtlWeighment.weighment_id == weighment_id).first() is None

    # Delete Ex-Bond entry
    resp = authenticated_client.delete(f"/api/weboc/ex-bond/{entry_id}")
    assert resp.status_code == 200
    assert db_session.query(ExBondEntry).filter(ExBondEntry.entry_id == entry_id).first() is None


def test_validation_error_returns_clean_string_detail(authenticated_client):
    # Check invalid declaration type override trigger a ValidationError
    resp = authenticated_client.put("/api/weboc/99999/declaration-type", json={
        "gd_type": "INVALID_TYPE"
    })
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)

    # Check negative weight trigger a ValidationError in weighment save
    resp = authenticated_client.post("/api/weboc/99999/kgtl", json={
        "own_weight_kg": -100
    })
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
