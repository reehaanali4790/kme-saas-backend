"""Auth tests: login, logout/session invalidation, refresh, login rate limiting,
and the existing check_permission-based authorization pattern (require_admin).
"""


def test_login_success(client, make_user):
    user, password = make_user(role_name="ADMIN")
    resp = client.post("/api/auth/login", data={"username": user.username, "password": password})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == user.username
    assert body["user"]["role"] == "ADMIN"
    assert "access_token" in body and "refresh_token" in body


def test_login_wrong_password(client, make_user):
    user, _password = make_user(role_name="ADMIN")
    resp = client.post("/api/auth/login", data={"username": user.username, "password": "wrong-password"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/api/auth/login", data={"username": "does-not-exist", "password": "whatever"})
    assert resp.status_code == 401


def test_login_rate_limited_after_threshold(client, make_user):
    user, _password = make_user(role_name="ADMIN")

    statuses = [
        client.post("/api/auth/login", data={"username": user.username, "password": "wrong"}).status_code
        for _ in range(15)
    ]

    assert 401 in statuses, f"expected some plain 401s before the limit trips, got: {statuses}"
    assert 429 in statuses, f"expected a 429 once the rate limit is exceeded, got: {statuses}"


def test_logout_invalidates_token(client, make_user):
    """Regression test: a JWT must stop authenticating once its session is logged
    out, even though the token itself hasn't expired yet.
    """
    user, password = make_user(role_name="ADMIN")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/auth/me", headers=headers).status_code == 200

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200

    after_logout = client.get("/api/auth/me", headers=headers)
    assert after_logout.status_code == 401


def test_refresh_issues_a_working_token(client, make_user):
    """Regression test: the token /refresh issues must itself be usable - it
    needs its own session record, not just the original login's.
    """
    user, password = make_user(role_name="ADMIN")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    refresh_token = login.json()["refresh_token"]

    refreshed = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200
    new_token = refreshed.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert me.status_code == 200
    assert me.json()["username"] == user.username


def test_refresh_rejects_an_access_token(client, make_user):
    user, password = make_user(role_name="ADMIN")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    access_token = login.json()["access_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


def test_viewer_cannot_access_admin_route(client, make_user):
    user, password = make_user(role_name="VIEWER")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]

    resp = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_admin_can_access_admin_route(authenticated_client):
    resp = authenticated_client.get("/api/admin/users")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_unauthenticated_request_is_rejected(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_login_sets_httponly_cookies(client, make_user):
    user, password = make_user(role_name="ADMIN")
    resp = client.post("/api/auth/login", data={"username": user.username, "password": password})
    assert resp.status_code == 200
    
    # Check cookies are set
    cookies = resp.cookies
    assert "access_token" in cookies
    assert "refresh_token" in cookies
    assert "csrf_token" in cookies


def test_csrf_cookie_protection(client, make_user):
    user, password = make_user(role_name="ADMIN")
    
    # 1. Login sets cookies (access_token, csrf_token) in client session
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    assert login.status_code == 200
    csrf_token = login.json()["csrf_token"]
    
    # 2. POST request without CSRF header must fail with 403
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "CSRF token validation failed"
    
    # 3. POST request with correct CSRF header must pass
    resp = client.post("/api/auth/logout", headers={"x-csrf-token": csrf_token})
    assert resp.status_code == 200


def test_login_sql_injection_defense(client):
    """Verify that SQL injection payloads do not bypass login or cause db exceptions."""
    payloads = [
        "' OR '1'='1",
        "admin' --",
        "admin' #",
        "' UNION SELECT NULL, NULL, NULL --",
        "'; DROP TABLE users; --"
    ]
    for payload in payloads:
        # Try as username
        resp = client.post("/api/auth/login", data={"username": payload, "password": "password"})
        assert resp.status_code == 401
        
        # Try as password
        resp = client.post("/api/auth/login", data={"username": "admin", "password": payload})
        assert resp.status_code == 401


def test_lookup_sql_injection_defense(client, make_user):
    """Verify that SQL injection payloads on lookup parameters are treated as literal text."""
    user, password = make_user(role_name="ADMIN")
    login = client.post("/api/auth/login", data={"username": user.username, "password": password})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    payload = "supplier' UNION SELECT id, username FROM users; --"
    resp = client.get(f"/api/lookup/supplier?q={payload}", headers=headers)
    assert resp.status_code == 200
    
    # It must return an empty list because the search filters literally for that payload string
    # rather than running the SQL union injection.
    assert isinstance(resp.json(), list)
    assert len(resp.json()) == 0
