"""Tests for the demurrage config module migration (modules/shipments/demurrage_router.py,
demurrage_service.py, demurrage_schemas.py)."""
import pytest
from pydantic import ValidationError as PydanticValidationError

from modules.shipments.demurrage_schemas import DemurrageConfigUpdate


# ---------------------------------------------------------------------------
# Schema-level: this module validates STRICTLY (matches the original's explicit
# HTTPException(400) on a malformed value, unlike the lenient-parse pattern used
# elsewhere)
# ---------------------------------------------------------------------------

def test_malformed_free_days_is_rejected_strictly():
    with pytest.raises(PydanticValidationError):
        DemurrageConfigUpdate(free_days="not-a-number")


def test_malformed_per_day_charge_is_rejected_strictly():
    with pytest.raises(PydanticValidationError):
        DemurrageConfigUpdate(per_day_charge="garbage")


def test_valid_values_pass():
    data = DemurrageConfigUpdate(free_days=10, per_day_charge="50.5", warn_days=5)
    assert data.free_days == 10
    assert data.warn_days == 5


def test_compute_demurrage_stops_on_delivered_shipment():
    from datetime import date
    from types import SimpleNamespace
    from modules.shipments.demurrage_service import compute_demurrage

    shipment = SimpleNamespace(status="DELIVERED", delivery_date=date(2026, 3, 20), on_port_date=None, eta=None)
    bl = SimpleNamespace(
        demurrage_start_date=date(2026, 3, 1),
        bl_date=None,
        free_days=14,
        demurrage_total_amount=None,
        demurrage_currency="USD",
        demurrage_cleared_date=None,
        shipment=shipment,
    )
    cfg = SimpleNamespace(free_days=7, warn_days=3, currency="USD", per_day_charge=100)

    dem = compute_demurrage(bl, cfg, today=date(2026, 4, 1))
    assert dem["state"] == "CLEARED"
    assert dem["chargeable_days"] == 5  # last free Mar 15, stop Mar 20
    assert dem["accrued_charge"] == 0.0
    assert dem["can_enter_amount"] is True


def test_compute_demurrage_delivered_without_dates_still_cleared():
    from datetime import date
    from types import SimpleNamespace
    from modules.shipments.demurrage_service import compute_demurrage

    shipment = SimpleNamespace(status="DELIVERED", delivery_date=date(2026, 3, 20), on_port_date=None, eta=None)
    bl = SimpleNamespace(
        demurrage_start_date=None,
        bl_date=None,
        free_days=None,
        demurrage_total_amount=None,
        demurrage_currency="USD",
        demurrage_cleared_date=None,
        shipment=shipment,
    )
    cfg = SimpleNamespace(free_days=7, warn_days=3, currency="USD", per_day_charge=100)

    dem = compute_demurrage(bl, cfg, today=date(2026, 4, 1))
    assert dem["state"] == "CLEARED"
    assert dem["can_enter_amount"] is True


def test_compute_demurrage_accruing_no_formula_estimate():
    from datetime import date
    from types import SimpleNamespace
    from modules.shipments.demurrage_service import compute_demurrage

    shipment = SimpleNamespace(status="PENDING", delivery_date=None, on_port_date=None, eta=None)
    bl = SimpleNamespace(
        demurrage_start_date=date(2026, 3, 1),
        bl_date=None,
        free_days=14,
        demurrage_total_amount=None,
        demurrage_currency="USD",
        demurrage_cleared_date=None,
        shipment=shipment,
    )
    cfg = SimpleNamespace(free_days=7, warn_days=3, currency="USD", per_day_charge=100)

    dem = compute_demurrage(bl, cfg, today=date(2026, 3, 20))
    assert dem["state"] == "ACCRUING"
    assert dem["chargeable_days"] == 5
    assert dem["accrued_charge"] == 0.0
    assert dem["can_enter_amount"] is False


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_write(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/demurrage/config", headers=headers).status_code == 200
    assert client.get("/api/demurrage/at-risk", headers=headers).status_code == 200

    resp = client.put("/api/demurrage/config", headers=headers, json={"free_days": 10})
    assert resp.status_code == 403


def test_operator_can_update_config(authenticated_client):
    got = authenticated_client.get("/api/demurrage/config")
    assert got.status_code == 200

    updated = authenticated_client.put("/api/demurrage/config", json={
        "free_days": 14, "per_day_charge": "75.50", "currency": "usd", "warn_days": 5,
    })
    assert updated.status_code == 200
    cfg = updated.json()["config"]
    assert cfg["free_days"] == 14
    assert cfg["per_day_charge"] == 75.5
    assert cfg["currency"] == "USD"

    got2 = authenticated_client.get("/api/demurrage/config")
    assert got2.json()["free_days"] == 14


def test_malformed_config_update_returns_clean_string_detail(authenticated_client):
    resp = authenticated_client.put("/api/demurrage/config", json={"free_days": "garbage"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
