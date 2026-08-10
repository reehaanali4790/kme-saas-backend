"""Tests for the Reports module migration (modules/reports/router.py, service.py,
schemas.py, lookup_router.py, lookup_service.py, lookup_schemas.py,
dashboard_router.py, dashboard_service.py)."""
import pytest

from modules.reports.service import norm_vessel, norm_bank, item_type, bulk_update_vessel
from infrastructure.normalization.normalization_service import matches_company_code, CompanyResolver


def test_matches_company_code_accepts_any_type(db_session):
    resolver = CompanyResolver(db_session)
    # None or empty filter code always matches
    assert matches_company_code(resolver, "Perfect Craft Pvt Ltd", None) is True
    assert matches_company_code(resolver, 12345, None) is True
    assert matches_company_code(resolver, None, None) is True

    # Matching with filter code handles non-string / dict.get values without raising type error
    r = {"importer": "Perfect Craft Pvt Ltd", "order_qty": 12.5}
    assert matches_company_code(resolver, r.get("importer"), "PCL") is True
    assert matches_company_code(resolver, r.get("order_qty"), "PCL") is False

from modules.reports.schemas import VesselBulkUpdate


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

def test_norm_vessel_collapses_voyage_variants():
    assert norm_vessel("EFFIE") == "EFFIE"
    assert norm_vessel("EFFIE V") == "EFFIE V"
    assert norm_vessel("EFFIE V.") == "EFFIE V"
    assert norm_vessel("EFFIE VOYAGE 12") == "EFFIE"


def test_norm_vessel_does_not_strip_real_words_starting_with_v():
    assert norm_vessel("MSC VICTORY") == "MSC VICTORY"


def test_norm_vessel_none():
    assert norm_vessel(None) is None


def test_norm_bank_known_alias():
    assert norm_bank("Habib Bank Limited - Main Branch") == "HBL"
    assert norm_bank("MCB ISLAMIC BANK LTD") == "MCB Islamic Bank"


def test_norm_bank_unknown_falls_back_to_title_case():
    assert norm_bank("Some Random Bank Pvt Ltd") == "Some Random Bank"


def test_norm_bank_blank():
    assert norm_bank(None) == "(Unknown Bank)"
    assert norm_bank("") == "(Unknown Bank)"


def test_item_type_no_products():
    assert item_type(None) == "(Unassigned)"


def test_bulk_update_vessel_requires_vessel(db_session):
    with pytest.raises(ValueError, match="vessel is required"):
        bulk_update_vessel(db_session, VesselBulkUpdate(vessel=""), "tester")


def test_bulk_update_vessel_rejects_malformed_eta(db_session):
    with pytest.raises(ValueError, match="eta must be YYYY-MM-DD"):
        bulk_update_vessel(db_session, VesselBulkUpdate(vessel="EFFIE", eta="not-a-date"), "tester")


def test_bulk_update_vessel_requires_eta_or_port_status(db_session):
    with pytest.raises(ValueError, match="Provide eta, port_status, on_port_date"):
        bulk_update_vessel(db_session, VesselBulkUpdate(vessel="EFFIE"), "tester")


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_vessel_bulk_update_lifecycle(authenticated_client, db_session):
    from models.database_models import Shipment
    ship = Shipment(shipment_ref="REP-TEST-001", vessel_name="TESTVESSEL")
    db_session.add(ship)
    db_session.commit()

    resp = authenticated_client.post("/api/reports/vessel/bulk-update", json={
        "vessel": "TESTVESSEL", "eta": "2026-08-01", "port_status": "ARRIVED",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    db_session.refresh(ship)
    assert str(ship.eta) == "2026-08-01"
    assert ship.vessel_location == "ARRIVED"


def test_vessel_bulk_update_departure_sets_demurrage_start(authenticated_client, db_session):
    from datetime import date
    from models.database_models import Shipment, BillOfLading

    ship1 = Shipment(shipment_ref="REP-DEP-1", vessel_name="DEPVESSEL")
    ship2 = Shipment(shipment_ref="REP-DEP-2", vessel_name="DEPVESSEL")
    db_session.add_all([ship1, ship2])
    db_session.flush()
    bl1 = BillOfLading(
        shipment_id=ship1.shipment_id,
        bl_type="COIL",
        demurrage_start_date=date(2025, 1, 1),
    )
    bl2 = BillOfLading(
        shipment_id=ship2.shipment_id,
        bl_type="COIL",
        demurrage_start_date=date(2025, 2, 1),
    )
    db_session.add_all([bl1, bl2])
    db_session.commit()

    resp = authenticated_client.post("/api/reports/vessel/bulk-update", json={
        "vessel": "DEPVESSEL",
        "departure_date": "2026-08-05",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 2
    assert body.get("demurrage_bls_updated") == 2

    db_session.refresh(bl1)
    db_session.refresh(bl2)
    assert bl1.demurrage_start_date == date(2026, 8, 5)
    assert bl2.demurrage_start_date == date(2026, 8, 5)


def test_vessel_bulk_update_missing_vessel_returns_400(authenticated_client):
    resp = authenticated_client.post("/api/reports/vessel/bulk-update", json={"vessel": ""})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_list_vessels(authenticated_client):
    resp = authenticated_client.get("/api/reports/vessels")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_bank_report_smoke(authenticated_client):
    resp = authenticated_client.get("/api/reports/banks")
    assert resp.status_code == 200
    assert "banks" in resp.json()


def test_buyer_report_smoke(authenticated_client):
    resp = authenticated_client.get("/api/reports/buyers")
    assert resp.status_code == 200
    assert "buyers" in resp.json()


def test_gd_balance_smoke(authenticated_client):
    resp = authenticated_client.get("/api/reports/gd-balance")
    assert resp.status_code == 200
    assert "options" in resp.json()


def test_dashboard_summary(authenticated_client):
    resp = authenticated_client.get("/api/dashboard/summary")
    assert resp.status_code == 200
    assert "kpis" in resp.json()


def test_dashboard_v2_summary(authenticated_client):
    resp = authenticated_client.get("/api/dashboard/v2/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "kpis" in body
    assert "eta_dashboard" in body
    assert "shipment_table" in body


def test_dashboard_arrivals(authenticated_client):
    resp = authenticated_client.get("/api/dashboard/arrivals")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_lookup_unknown_kind_returns_404(authenticated_client):
    resp = authenticated_client.get("/api/lookup/bogus")
    assert resp.status_code == 404


def test_lookup_add_blank_name_returns_400(authenticated_client):
    resp = authenticated_client.post("/api/lookup/importer", json={"name": "  "})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_lookup_add_and_search_dedup(authenticated_client):
    created = authenticated_client.post("/api/lookup/importer", json={"name": "Test Importer Co"})
    assert created.status_code == 200
    assert created.json()["created"] is True

    dup = authenticated_client.post("/api/lookup/importer", json={"name": "test importer co"})
    assert dup.status_code == 200
    assert dup.json()["created"] is False
    assert dup.json()["id"] == created.json()["id"]

    found = authenticated_client.get("/api/lookup/importer?q=Test Importer")
    assert found.status_code == 200
    assert any(r["id"] == created.json()["id"] for r in found.json())
