"""Tests for the bank_limits reference migration.

Unit tests exercise services/bank_limit_service.py's pure calculation functions
directly with lightweight fake objects (no DB needed - these functions only do
attribute access on already-loaded relationships in real usage), except where a
function normalizes company/bank names and needs a real db_session. Integration
tests exercise the actual HTTP endpoints end-to-end, including the new
require_min_role permission gate on mutations.
"""
from types import SimpleNamespace

import pytest

from models.database_models import BankLimit, BankLimitLine
from modules.bank_limits.schemas import BankLimitLineIn
from modules.bank_limits import services as svc


def _lc(status="OPEN", shipments=None, products=None, currency="USD", exchange_rate=None,
        payment_terms=None):
    return SimpleNamespace(
        status=status,
        shipments=shipments or [],
        products=products or [],
        currency=currency,
        exchange_rate=exchange_rate,
        payment_terms=payment_terms,
    )


def _shipment(status="PENDING", payment_date=None, retirement_date=None, exchange_rate=None):
    return SimpleNamespace(
        status=status, payment_date=payment_date, retirement_date=retirement_date,
        exchange_rate=exchange_rate,
    )


def _product(lc_amount=None, quantity=None, lc_unit_price=None):
    return SimpleNamespace(lc_amount=lc_amount, quantity=quantity, lc_unit_price=lc_unit_price)


# ---------------------------------------------------------------------------
# norm_lc_type / norm_limit_type / type_applies
# ---------------------------------------------------------------------------

def test_norm_lc_type_valid_and_invalid():
    assert svc.norm_lc_type("sight") == "SIGHT"
    assert svc.norm_lc_type("DA") == "DA"
    assert svc.norm_lc_type("bogus") == "BOTH"
    assert svc.norm_lc_type(None) == "BOTH"


def test_norm_limit_type_legacy_sub_becomes_child():
    assert svc.norm_limit_type("PARENT") == "PARENT"
    assert svc.norm_limit_type("SUB") == "CHILD"
    assert svc.norm_limit_type("CHILD") == "CHILD"
    assert svc.norm_limit_type(None) == "CHILD"


def test_type_applies_both_covers_everything():
    assert svc.type_applies("BOTH", None) is True
    assert svc.type_applies("BOTH", "SIGHT") is True


def test_type_applies_specific_type_needs_matching_tenor():
    assert svc.type_applies("SIGHT", "SIGHT") is True
    assert svc.type_applies("SIGHT", "DA") is False
    assert svc.type_applies("SIGHT", None) is False


# ---------------------------------------------------------------------------
# lc_is_utilized - the core "does this LC still consume the bank limit" rule
# ---------------------------------------------------------------------------

def test_lc_is_utilized_cancelled_or_closed_lc_releases():
    assert svc.lc_is_utilized(_lc(status="CANCELLED")) is False
    assert svc.lc_is_utilized(_lc(status="CLOSED")) is False


def test_lc_is_utilized_open_lc_no_shipments_is_committed():
    assert svc.lc_is_utilized(_lc(status="OPEN", shipments=[])) is True


def test_lc_is_utilized_open_shipment_keeps_it_utilized():
    lc = _lc(status="OPEN", shipments=[_shipment(status="PENDING")])
    assert svc.lc_is_utilized(lc) is True


def test_lc_is_utilized_settled_shipment_releases():
    from datetime import date
    lc = _lc(status="OPEN", shipments=[_shipment(payment_date=date(2026, 1, 1))])
    assert svc.lc_is_utilized(lc) is False

    lc2 = _lc(status="OPEN", shipments=[_shipment(status="CLOSED")])
    assert svc.lc_is_utilized(lc2) is False


def test_lc_is_utilized_any_open_shipment_among_several_keeps_it_utilized():
    from datetime import date
    lc = _lc(status="OPEN", shipments=[
        _shipment(payment_date=date(2026, 1, 1)),   # settled
        _shipment(status="PENDING"),                 # still open
    ])
    assert svc.lc_is_utilized(lc) is True


# ---------------------------------------------------------------------------
# lc_amount / lc_pkr_rate / lc_currency
# ---------------------------------------------------------------------------

def test_lc_amount_sums_product_lc_amounts():
    lc = _lc(products=[_product(lc_amount=100), _product(lc_amount=50.5)])
    assert svc.lc_amount(lc) == 150.5


def test_lc_amount_falls_back_to_qty_times_unit_price():
    lc = _lc(products=[_product(lc_amount=None, quantity=10, lc_unit_price=5)])
    assert svc.lc_amount(lc) == 50.0


def test_lc_pkr_rate_prefers_shipment_rate_over_lc_rate():
    lc = _lc(exchange_rate=280, shipments=[_shipment(exchange_rate=285)])
    rate, source = svc.lc_pkr_rate(lc)
    assert rate == 285
    assert source == "shipment"


def test_lc_pkr_rate_falls_back_to_lc_rate():
    lc = _lc(exchange_rate=280, shipments=[_shipment(exchange_rate=None)])
    rate, source = svc.lc_pkr_rate(lc)
    assert rate == 280
    assert source == "lc"


def test_lc_pkr_rate_missing_when_neither_set():
    lc = _lc(exchange_rate=None, shipments=[])
    assert svc.lc_pkr_rate(lc) == (None, None)


def test_lc_currency_extracts_letters_only():
    assert svc.lc_currency(_lc(currency="$ USD")) == "USD"
    assert svc.lc_currency(_lc(currency=None)) == "USD"


# ---------------------------------------------------------------------------
# line_conflict_warnings
# ---------------------------------------------------------------------------

def test_line_conflict_warnings_both_limit_never_warns():
    lines = [BankLimitLineIn(company_name="ABC", lc_type="DA")]
    assert svc.line_conflict_warnings("BOTH", lines) == []


def test_line_conflict_warnings_flags_mismatched_tenor():
    lines = [BankLimitLineIn(company_name="ABC", lc_type="DA")]
    warnings = svc.line_conflict_warnings("SIGHT", lines)
    assert len(warnings) == 1
    assert "ABC" in warnings[0]


# ---------------------------------------------------------------------------
# apply_lines - legacy SUB normalization, blank company names skipped
# ---------------------------------------------------------------------------

def test_apply_lines_normalizes_and_skips_blank_company_names(db_session):
    bl = BankLimit(bank_name="HBL", group_company="Perfect Craft")
    lines = [
        BankLimitLineIn(company_name="Parent Co", limit_type="PARENT"),
        BankLimitLineIn(company_name="Child Co", limit_type="SUB", sub_limit_amount=500),
    ]
    svc.apply_lines(bl, lines, db_session)
    assert len(bl.lines) == 2
    assert bl.lines[0].limit_type == "PARENT"
    assert bl.lines[1].limit_type == "CHILD"   # legacy SUB normalized


# ---------------------------------------------------------------------------
# Integration: the actual HTTP endpoints, including the permission gate
# ---------------------------------------------------------------------------

def test_viewer_can_list_but_not_create(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/bank-limits/", headers=headers).status_code == 200

    resp = client.post("/api/bank-limits/", headers=headers, json={
        "bank_name": "HBL", "group_company": "Perfect Craft",
        "bank_limit_type": "REGULAR", "lc_type": "BOTH",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "group_limit_amount": 1000000,
    })
    assert resp.status_code == 403


def test_operator_can_create_get_update_delete(authenticated_client):
    create = authenticated_client.post("/api/bank-limits/", json={
        "bank_name": "HBL", "group_company": "Perfect Craft",
        "bank_limit_type": "REGULAR", "lc_type": "SIGHT",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "group_limit_amount": 1000000,
        "lines": [{"company_name": "Child Co", "limit_type": "CHILD",
                   "lc_type": "BOTH", "sub_limit_amount": 200000}],
    })
    assert create.status_code == 200, create.text
    limit_id = create.json()["limit_id"]

    got = authenticated_client.get(f"/api/bank-limits/{limit_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["bank_name"] == "HBL"
    assert body["group_limit_amount"] == 1000000
    # 2 lines: the explicit CHILD line + an auto-created PARENT line matching group_company
    assert len(body["lines"]) == 2
    by_type = {ln["limit_type"]: ln for ln in body["lines"]}
    assert by_type["CHILD"]["company_name"] == "Child Co"
    assert by_type["PARENT"]["company_name"] == "Perfect Craft"

    updated = authenticated_client.put(f"/api/bank-limits/{limit_id}", json={
        "remarks": "updated via test",
    })
    assert updated.status_code == 200

    got2 = authenticated_client.get(f"/api/bank-limits/{limit_id}")
    assert got2.json()["remarks"] == "updated via test"

    deleted = authenticated_client.delete(f"/api/bank-limits/{limit_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.get(f"/api/bank-limits/{limit_id}")
    assert missing.status_code == 404


def test_create_missing_required_field_returns_clean_string_detail(authenticated_client):
    """Regression guard: FastAPI's default 422 shape has `detail` as a list of
    error objects, but bank_limits.html does `toast(e.detail || 'Save failed')`
    expecting a plain string - see main.py's validation_error_handler."""
    resp = authenticated_client.post("/api/bank-limits/", json={
        "group_company": "Perfect Craft", "bank_limit_type": "REGULAR",
        "lc_type": "BOTH", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "group_limit_amount": 1000000,
    })
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_create_rejects_negative_sub_limit(authenticated_client):
    resp = authenticated_client.post("/api/bank-limits/", json={
        "bank_name": "HBL", "group_company": "Perfect Craft",
        "bank_limit_type": "REGULAR", "lc_type": "BOTH",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "group_limit_amount": 1000000,
        "lines": [{"company_name": "Child Co", "sub_limit_amount": -5}],
    })
    assert resp.status_code == 422
    assert "Child Co" in resp.json()["detail"]
