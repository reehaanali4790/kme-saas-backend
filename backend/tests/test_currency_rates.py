"""Tests for the Currency Rates module migration (modules/currency_rates/router.py,
service.py, schemas.py, lme_rates_router.py, lme_rates_service.py,
lme_calculation_router.py, lme_calculation_service.py, lme_calculation_schemas.py)."""
import pytest

from modules.currency_rates.schemas import CurrencyRateCreate
from modules.currency_rates.service import create_rate, update_rate
from modules.currency_rates.schemas import CurrencyRateUpdate
from core.exceptions import NotFoundError


# ---------------------------------------------------------------------------
# currency_rates/service.py — business-rule validation (ValueError -> 400)
# ---------------------------------------------------------------------------

def test_create_rate_rejects_duplicate_date(db_session, make_user):
    user, _ = make_user(role_name="ADMIN")
    create_rate(db_session, CurrencyRateCreate(rate_date="2026-02-01", usd_rate=280, eur_rate=300),
               user.user_id)
    with pytest.raises(ValueError, match="already exists"):
        create_rate(db_session, CurrencyRateCreate(rate_date="2026-02-01", usd_rate=280, eur_rate=300),
                    user.user_id)


def test_create_rate_rejects_eur_not_greater_than_usd(db_session, make_user):
    user, _ = make_user(role_name="ADMIN")
    with pytest.raises(ValueError, match="EUR rate must be greater"):
        create_rate(db_session, CurrencyRateCreate(rate_date="2026-02-02", usd_rate=300, eur_rate=280),
                    user.user_id)


def test_update_rate_missing_raises_not_found(db_session, make_user):
    user, _ = make_user(role_name="ADMIN")
    with pytest.raises(NotFoundError):
        update_rate(db_session, 999999, CurrencyRateUpdate(usd_rate=280), user.user_id)


# ---------------------------------------------------------------------------
# Integration: currency rates CRUD + permission gates
# ---------------------------------------------------------------------------

def test_viewer_can_read_but_not_create(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/currency/rates/list", headers=headers).status_code == 200
    assert client.get("/api/currency/rates/latest", headers=headers).status_code == 200

    resp = client.post("/api/currency/rates/create", headers=headers,
                       json={"rate_date": "2026-05-01", "usd_rate": 280, "eur_rate": 300})
    assert resp.status_code == 403


def test_viewer_blocked_from_delete(make_user, authenticated_client):
    created = authenticated_client.post("/api/currency/rates/create",
                                        json={"rate_date": "2026-05-02", "usd_rate": 280, "eur_rate": 300})
    rate_id = created.json()["data"]["rate_id"]

    user, password = make_user(role_name="VIEWER")
    login = authenticated_client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = authenticated_client.delete(f"/api/currency/rates/{rate_id}", headers=headers)
    assert resp.status_code == 403


def test_create_get_update_delete_lifecycle(authenticated_client):
    create = authenticated_client.post("/api/currency/rates/create", json={
        "rate_date": "2026-06-01", "usd_rate": 280.5, "eur_rate": 305.0,
    })
    assert create.status_code == 200, create.text
    rate_id = create.json()["data"]["rate_id"]

    got = authenticated_client.get("/api/currency/rates/date/2026-06-01")
    assert got.status_code == 200
    assert got.json()["found"] is True
    assert got.json()["data"]["usd_rate"] == 280.5

    updated = authenticated_client.put(f"/api/currency/rates/{rate_id}", json={"usd_rate": 282.0})
    assert updated.status_code == 200

    deleted = authenticated_client.delete(f"/api/currency/rates/{rate_id}")
    assert deleted.status_code == 200

    missing = authenticated_client.delete(f"/api/currency/rates/{rate_id}")
    assert missing.status_code == 404


def test_create_duplicate_date_returns_400(authenticated_client):
    authenticated_client.post("/api/currency/rates/create",
                              json={"rate_date": "2026-06-05", "usd_rate": 280, "eur_rate": 300})
    dup = authenticated_client.post("/api/currency/rates/create",
                                    json={"rate_date": "2026-06-05", "usd_rate": 280, "eur_rate": 300})
    assert dup.status_code == 400
    assert isinstance(dup.json()["detail"], str)


def test_get_rate_by_date_not_found_returns_found_false(authenticated_client):
    resp = authenticated_client.get("/api/currency/rates/date/2099-01-01")
    assert resp.status_code == 200
    assert resp.json()["found"] is False


# ---------------------------------------------------------------------------
# LME rates matrix (read-only, no permission gate — matches original)
# ---------------------------------------------------------------------------

def test_lme_rates_matrix_default_group(authenticated_client):
    resp = authenticated_client.get("/api/lme-rates/matrix")
    assert resp.status_code == 200
    assert resp.json()["group"] == "HR"
    assert "matrix" in resp.json()


def test_lme_rates_matrix_invalid_group_falls_back_to_hr(authenticated_client):
    resp = authenticated_client.get("/api/lme-rates/matrix?group=BOGUS")
    assert resp.status_code == 200
    assert resp.json()["group"] == "HR"


# ---------------------------------------------------------------------------
# LME calculation (read/write, no permission gate — matches original, open to
# any authenticated user since it only recomputes derived values)
# ---------------------------------------------------------------------------

def test_lcs_for_calculation_list(authenticated_client):
    resp = authenticated_client.get("/api/calculate/lcs-for-calculation")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_calculate_lc_not_found_returns_400_not_500(authenticated_client):
    resp = authenticated_client.post("/api/calculate/calculate/999999")
    assert resp.status_code == 400


def test_bulletin_impact_missing_bulletin_returns_404(authenticated_client):
    resp = authenticated_client.get("/api/calculate/bulletin-impact/999999")
    assert resp.status_code == 404


def test_test_formula_match_valid_combo(authenticated_client):
    resp = authenticated_client.post("/api/calculate/test-match", json={
        "product_code": "HRP", "origin": "CHINA", "quality": "PRIME",
    })
    assert resp.status_code == 200
    assert resp.json()["formula_number"] == 1


def test_test_formula_match_raises_when_no_formula_matches(db_session, monkeypatch):
    """FormulaEngine.determine_formula always defaults to formula 1 in practice, so this
    exercises the service's guard clause directly rather than via a real unmatched input."""
    from modules.currency_rates.lme_calculation_service import test_formula_match
    from infrastructure.formula_engine import formula_engine

    monkeypatch.setattr(formula_engine.FormulaEngine, "determine_formula", staticmethod(lambda *a: None))
    with pytest.raises(ValueError, match="Could not match formula"):
        test_formula_match(db_session, "X", "Y", "Z")


def test_apply_rates_empty_lc_ids_processes_window(authenticated_client):
    resp = authenticated_client.post("/api/calculate/apply-rates", json={
        "bulletin_id": 999999, "lc_ids": [],
    })
    assert resp.status_code == 200
    assert resp.json()["applied"] == 0
