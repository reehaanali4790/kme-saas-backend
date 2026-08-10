"""Tests for the PDF bulletin upload module migration (modules/documents/pdf_upload_router.py,
pdf_upload_service.py). Unlike the other documents/ submodules, this one uses fine-grained
require_permission("upload_pdf" / "manage_users") gates (mirroring the original's manual
AuthService.check_permission calls) rather than require_min_role, since it already had a
matching permission flag."""
import pytest

from modules.documents.pdf_upload_service import (
    is_valid_pdf, get_region_from_symbol, resolve_currency_rate,
)


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

def test_is_valid_pdf():
    assert is_valid_pdf("bulletin.pdf") is True
    assert is_valid_pdf("bulletin.PDF") is True
    assert is_valid_pdf("bulletin.docx") is False


def test_get_region_from_symbol_known():
    assert get_region_from_symbol("MB-STE-0144") == "CHINA"
    assert get_region_from_symbol("MB-STE-0181") == "USA"


def test_get_region_from_symbol_unknown():
    assert get_region_from_symbol("UNKNOWN-SYMBOL") == "UNKNOWN"


def test_resolve_currency_rate_invalid_rate_id_raises_value_error(db_session):
    with pytest.raises(ValueError, match="Invalid rate_id"):
        resolve_currency_rate(db_session, "2026-01-01", rate_id=999999,
                              usd_rate=None, eur_rate=None, user_id=1, username="tester")


def test_resolve_currency_rate_missing_both_raises_value_error(db_session):
    with pytest.raises(ValueError, match="Currency rates required"):
        resolve_currency_rate(db_session, "2026-01-01", rate_id=None,
                              usd_rate=None, eur_rate=None, user_id=1, username="tester")


def test_resolve_currency_rate_creates_new_rate(db_session, make_user):
    user, _ = make_user(role_name="ADMIN")
    rate = resolve_currency_rate(db_session, "2026-03-03", rate_id=None,
                                 usd_rate=280.5, eur_rate=305.2,
                                 user_id=user.user_id, username="tester")
    assert rate.usd_rate == 280.5
    assert rate.source == "PDF Upload"


# ---------------------------------------------------------------------------
# Integration: permission gates
# ---------------------------------------------------------------------------

def test_viewer_blocked_from_upload_pdf_endpoints(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/pdf/check-rates", headers=headers,
                       files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert resp.status_code == 403


def test_operator_can_reach_upload_pdf_gate(client, make_user):
    """OPERATOR has can_upload_pdf=True - should pass the permission gate (may still
    fail later on invalid PDF content, but must not be blocked by permissions)."""
    user, password = make_user(role_name="OPERATOR")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post("/api/pdf/check-rates", headers=headers,
                       files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert resp.status_code != 403


def test_viewer_blocked_from_delete_bulletin(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.delete("/api/pdf/bulletins/999999", headers=headers)
    assert resp.status_code == 403


def test_admin_delete_nonexistent_bulletin_returns_404(authenticated_client):
    resp = authenticated_client.delete("/api/pdf/bulletins/999999")
    assert resp.status_code == 404


def test_list_bulletins(authenticated_client):
    resp = authenticated_client.get("/api/pdf/bulletins/list")
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_upload_non_pdf_rejected(authenticated_client):
    resp = authenticated_client.post("/api/pdf/check-rates",
                                     files={"file": ("test.docx", b"not a pdf", "application/octet-stream")})
    assert resp.status_code == 400
