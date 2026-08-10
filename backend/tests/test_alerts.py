"""Tests for the Alerts module migration (modules/alerts/router.py, service.py,
schemas.py, engine_router.py, engine_service.py, expiries_router.py,
expiries_service.py)."""
from modules.alerts.expiries_service import _tone


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

def test_tone_expired():
    assert _tone(0) == "expired"
    assert _tone(-5) == "expired"


def test_tone_critical():
    assert _tone(7) == "critical"


def test_tone_upcoming():
    assert _tone(30) == "upcoming"


def test_tone_ok():
    assert _tone(31) == "ok"


def test_tone_unknown():
    assert _tone(None) == "unknown"


# ---------------------------------------------------------------------------
# Price alerts (modules/alerts/router.py) — permission gate + CRUD-ish flow
# ---------------------------------------------------------------------------

def test_alerts_list_smoke(authenticated_client):
    resp = authenticated_client.get("/api/alerts/list")
    assert resp.status_code == 200
    assert isinstance(resp.json()["items"], list)


def test_alerts_stats_smoke(authenticated_client):
    resp = authenticated_client.get("/api/alerts/stats/summary")
    assert resp.status_code == 200
    assert "stats" in resp.json()


def test_alerts_savings_opportunities_smoke(authenticated_client):
    resp = authenticated_client.get("/api/alerts/savings-opportunities")
    assert resp.status_code == 200


def test_alert_detail_not_found_returns_404(authenticated_client):
    resp = authenticated_client.get("/api/alerts/999999")
    assert resp.status_code == 404


def test_mark_viewed_not_found_returns_404(authenticated_client):
    resp = authenticated_client.post("/api/alerts/999999/mark-viewed")
    assert resp.status_code == 404


def test_take_action_requires_reopen_lc_permission(client, make_user):
    """OPERATOR has can_reopen_lc=False per tests/conftest.py's ROLE_PERMISSIONS -
    the original hand-checked check_permission(current_user, "reopen_lc")."""
    user, password = make_user(role_name="OPERATOR")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/alerts/999999/take-action", headers=headers,
                       json={"action": "NOTED"})
    assert resp.status_code == 403


def test_dismiss_all_smoke(authenticated_client):
    resp = authenticated_client.post("/api/alerts/dismiss-all")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_whatsapp_status_does_not_crash(authenticated_client):
    """Regression test for the pre-existing `from services import whatsapp_service`
    bug (services/ package no longer exists post-restructure) - this endpoint would
    500 with ModuleNotFoundError before the fix to infrastructure.whatsapp.whatsapp_service."""
    resp = authenticated_client.get("/api/alerts/whatsapp/status")
    assert resp.status_code == 200
    assert "configured" in resp.json()


def test_whatsapp_send_pending_does_not_crash(authenticated_client):
    resp = authenticated_client.post("/api/alerts/whatsapp/send-pending")
    assert resp.status_code == 200


def test_whatsapp_resend_not_found_returns_404(authenticated_client):
    resp = authenticated_client.post("/api/alerts/999999/send-whatsapp")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Alert engine (modules/alerts/engine_router.py) — operational SystemAlert CRUD
# ---------------------------------------------------------------------------

def test_engine_list_smoke(authenticated_client):
    resp = authenticated_client.get("/api/alert-engine/")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_engine_stats_smoke(authenticated_client):
    resp = authenticated_client.get("/api/alert-engine/stats")
    assert resp.status_code == 200
    assert "active_total" in resp.json()


def test_engine_acknowledge_not_found_returns_404(authenticated_client):
    resp = authenticated_client.post("/api/alert-engine/999999/acknowledge")
    assert resp.status_code == 404


def test_engine_dismiss_not_found_returns_404(authenticated_client):
    resp = authenticated_client.post("/api/alert-engine/999999/dismiss")
    assert resp.status_code == 404


def test_engine_scan_smoke(authenticated_client):
    resp = authenticated_client.post("/api/alert-engine/scan?send_whatsapp=false")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Expiries (modules/alerts/expiries_router.py) — read-only aggregation
# ---------------------------------------------------------------------------

def test_expiries_list_smoke(authenticated_client):
    resp = authenticated_client.get("/api/expiries/")
    assert resp.status_code == 200
    body = resp.json()
    assert "counts" in body
    assert "doc_types" in body
    assert len(body["doc_types"]) == 10


def test_expiries_filter_by_doc_type(authenticated_client):
    resp = authenticated_client.get("/api/expiries/?doc_type=LC_EXPIRY")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["doc_type"] == "LC_EXPIRY"


def test_expiries_exclude_ok(authenticated_client):
    resp = authenticated_client.get("/api/expiries/?include_ok=false")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["tone"] != "ok"
