"""Tests for the lc_creation module migration (schemas/lc_creation.py,
services/lc_creation_service.py, api/lc_creation_endpoints.py).
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from models.database_models import LCMaster, LCProduct
from modules.lc_creation.schemas import LCCreate


# ---------------------------------------------------------------------------
# Schema-level: messy AI-extracted input handling
# ---------------------------------------------------------------------------

def test_schema_extracts_leading_number_from_messy_amount():
    data = LCCreate(lc_number="LC-1", unit_price_usd="593.00 PER M/TON", amount="97,252")
    assert data.unit_price_usd == Decimal("593.00")
    assert data.amount == Decimal("97252")


def test_schema_tolerates_malformed_dates_as_none():
    """Matches the original _date() helper's silent-fallback behavior - AI extraction
    can produce garbage dates, and the request shouldn't be rejected outright for it."""
    data = LCCreate(lc_number="LC-1", issue_date="not-a-real-date")
    assert data.issue_date is None


def test_schema_requires_lc_number():
    with pytest.raises(PydanticValidationError):
        LCCreate(lc_number="   ")


def test_schema_strips_lc_number():
    data = LCCreate(lc_number="  LC-123  ")
    assert data.lc_number == "LC-123"


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoint, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_cannot_create_lc(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]

    resp = client.post("/api/lc-create/", headers={"Authorization": f"Bearer {token}"},
                        json={"lc_number": "LC-VIEWER-TEST"})
    assert resp.status_code == 403


def test_operator_can_create_minimal_lc(authenticated_client, db_session):
    resp = authenticated_client.post("/api/lc-create/", json={
        "lc_number": "LC-TEST-001",
        "applicant_name": "Test Importer Co",
        "beneficiary_name": "Test Supplier Co",
        "currency": "USD",
        "quantity_mt": "100",
        "unit_price_usd": "500.00 PER M/TON",
        "goods_description": "Steel Coils",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["lc_number"] == "LC-TEST-001"

    lc = db_session.query(LCMaster).filter(LCMaster.lc_id == body["lc_id"]).first()
    assert lc is not None
    assert lc.status == "OPEN"
    assert lc.source == "CONTRACT"

    product = db_session.query(LCProduct).filter(LCProduct.lc_id == lc.lc_id).first()
    assert product is not None
    assert product.lc_unit_price == Decimal("500.00")
    assert product.quantity == Decimal("100")


def test_create_lc_missing_lc_number_returns_clean_string_detail(authenticated_client):
    resp = authenticated_client.post("/api/lc-create/", json={"currency": "USD"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
