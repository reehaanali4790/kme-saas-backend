"""Tests for the Admin module (modules/admin/router.py — user/role/log management,
modules/admin/assistant_endpoints.py + assistant_schemas.py — AI assistant)."""


# ---------------------------------------------------------------------------
# AI Assistant (modules/admin/assistant_endpoints.py)
# ---------------------------------------------------------------------------

def test_ask_assistant_requires_question(authenticated_client):
    resp = authenticated_client.post("/api/assistant/ask", json={"question": "   "})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str)


def test_ask_assistant_missing_question_key(authenticated_client):
    resp = authenticated_client.post("/api/assistant/ask", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin — user/role/log management (modules/admin/router.py)
# ---------------------------------------------------------------------------

def test_non_admin_blocked_from_admin_endpoints(client, make_user):
    user, password = make_user(role_name="OPERATOR")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/api/admin/users", headers=headers).status_code == 403
    assert client.get("/api/admin/roles", headers=headers).status_code == 403
    assert client.get("/api/admin/logs", headers=headers).status_code == 403


def test_list_roles(authenticated_client):
    resp = authenticated_client.get("/api/admin/roles")
    assert resp.status_code == 200
    names = [r["role_name"] for r in resp.json()]
    assert "ADMIN" in names


def test_list_users_includes_self(authenticated_client):
    resp = authenticated_client.get("/api/admin/users")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_create_user_duplicate_username_rejected(authenticated_client, make_user):
    existing, _ = make_user(role_name="VIEWER")
    roles = authenticated_client.get("/api/admin/roles").json()
    role_name = next(r["role_name"] for r in roles if r["role_name"] == "VIEWER")

    resp = authenticated_client.post("/api/admin/users", json={
        "username": existing.username, "full_name": "Dup", "email": "dup@test.com",
        "password": "Test1234!", "role_name": role_name,
    })
    assert resp.status_code == 400


def test_create_user_accepts_role_id(authenticated_client, make_user):
    make_user(role_name="VIEWER")
    roles = authenticated_client.get("/api/admin/roles").json()
    role_id = next(r["role_id"] for r in roles if r["role_name"] == "VIEWER")

    created = authenticated_client.post("/api/admin/users", json={
        "username": "roleiduser", "full_name": "Role ID User",
        "email": "roleiduser@test.com", "password": "Test1234!", "role_id": role_id,
    })
    assert created.status_code == 201, created.text


def test_create_update_toggle_user_lifecycle(authenticated_client, make_user):
    make_user(role_name="VIEWER")  # ensures the VIEWER Role row exists in the test DB
    roles = authenticated_client.get("/api/admin/roles").json()
    role_name = next(r["role_name"] for r in roles if r["role_name"] == "VIEWER")

    created = authenticated_client.post("/api/admin/users", json={
        "username": "newtestuser", "full_name": "New Test User",
        "email": "newtestuser@test.com", "password": "Test1234!", "role_name": role_name,
    })
    assert created.status_code == 201, created.text
    user_id = created.json()["user_id"]

    updated = authenticated_client.put(f"/api/admin/users/{user_id}", json={
        "full_name": "Updated Name", "email": "newtestuser@test.com", "role_name": role_name,
    })
    assert updated.status_code == 200

    toggled = authenticated_client.post(f"/api/admin/users/{user_id}/toggle-active")
    assert toggled.status_code == 200
    assert toggled.json()["active"] is False

    reset = authenticated_client.post(f"/api/admin/users/{user_id}/reset-password",
                                      json={"new_password": "NewPass123!"})
    assert reset.status_code == 200


def test_cannot_deactivate_own_account(authenticated_client, db_session):
    from models.database_models import User
    me = db_session.query(User).order_by(User.user_id.desc()).first()
    resp = authenticated_client.post(f"/api/admin/users/{me.user_id}/toggle-active")
    assert resp.status_code == 400


def test_login_logs_smoke(authenticated_client):
    resp = authenticated_client.get("/api/admin/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert isinstance(body["total"], int)
