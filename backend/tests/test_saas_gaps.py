"""Isolation, auth, and scheduler coverage for remaining SaaS gaps."""

import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from config.database import SessionLocal, set_platform_search_path
from core.redis import redis_cache
from models.platform_models import Organization, OrganizationMembership, User
from modules.auth.services import AuthService
from modules.tenants.provision import destroy_tenant, provision_tenant


class _MockRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
        return True

    def keys(self, pattern):
        return list(self.store.keys())

    def ping(self):
        return True


@pytest.fixture
def mock_redis(monkeypatch):
    mock = _MockRedis()
    monkeypatch.setattr(redis_cache, "client", mock)
    monkeypatch.setattr(redis_cache, "enabled", True)
    return mock


def test_resolve_document_path_stays_in_tenant(tmp_path, monkeypatch):
    from infrastructure.documents import document_files as df

    upload = tmp_path / "uploads"
    (upload / "tenant_a" / "invoice_documents").mkdir(parents=True)
    (upload / "tenant_b" / "invoice_documents").mkdir(parents=True)
    a_file = upload / "tenant_a" / "invoice_documents" / "1_invoice.pdf"
    b_file = upload / "tenant_b" / "invoice_documents" / "1_invoice.pdf"
    a_file.write_bytes(b"aaa")
    b_file.write_bytes(b"bbb")

    monkeypatch.setattr(df.settings, "UPLOAD_DIR", str(upload))
    monkeypatch.setattr(df, "get_current_tenant_schema", lambda: "tenant_a")

    found = df.resolve_document_path("missing/1_invoice.pdf")
    assert found is not None
    assert os.path.normpath(found) == os.path.normpath(str(a_file))
    with open(found, "rb") as fh:
        assert fh.read() == b"aaa"


def test_resolve_document_path_no_cross_tenant_basename_search(tmp_path, monkeypatch):
    from infrastructure.documents import document_files as df

    upload = tmp_path / "uploads"
    (upload / "tenant_b" / "invoice_documents").mkdir(parents=True)
    b_file = upload / "tenant_b" / "invoice_documents" / "shared.pdf"
    b_file.write_bytes(b"secret")

    monkeypatch.setattr(df.settings, "UPLOAD_DIR", str(upload))
    monkeypatch.setattr(df, "get_current_tenant_schema", lambda: "tenant_a")

    assert df.resolve_document_path("shared.pdf") is None


def test_scheduler_leader_lock(mock_redis, monkeypatch):
    import core.scheduler_lock as lock

    mock_redis.store.clear()
    monkeypatch.setattr(lock, "_holder", False)
    assert lock.acquire_scheduler_leadership() is True
    assert lock.LOCK_KEY in mock_redis.store
    assert lock.acquire_scheduler_leadership() is False
    monkeypatch.setattr(lock, "_holder", True)
    lock.release_scheduler_leadership()
    assert lock.LOCK_KEY not in mock_redis.store


def test_forgot_password_always_ok(client, make_user, mock_redis):
    user, _ = make_user(role_name="ADMIN")
    unknown = client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert unknown.status_code == 200
    known = client.post("/api/auth/forgot-password", json={"email": user.email})
    assert known.status_code == 200
    assert any(k.startswith("lme:pwreset:") for k in mock_redis.store)


def test_reset_password_with_token(client, make_user, mock_redis):
    user, _ = make_user(role_name="ADMIN")
    mock_redis.store["lme:pwreset:abc123"] = str(user.user_id)
    bad = client.post("/api/auth/reset-password", json={"token": "nope", "new_password": "NewPass123!"})
    assert bad.status_code == 400
    ok = client.post("/api/auth/reset-password", json={"token": "abc123", "new_password": "NewPass123!"})
    assert ok.status_code == 200
    login = client.post("/api/auth/login", data={"username": user.username, "password": "NewPass123!"})
    assert login.status_code == 200


def test_accept_invite_sets_password(client, db_session, make_user):
    user, _ = make_user(role_name="ADMIN")
    membership = db_session.query(OrganizationMembership).filter(
        OrganizationMembership.user_id == user.user_id
    ).first()
    membership.invite_token = "invite-token-1"
    membership.invite_expires_at = datetime.utcnow() + timedelta(days=2)
    db_session.commit()

    resp = client.post(
        "/api/auth/accept-invite",
        json={"token": "invite-token-1", "password": "InvitePass9"},
    )
    assert resp.status_code == 200
    login = client.post("/api/auth/login", data={"username": user.username, "password": "InvitePass9"})
    assert login.status_code == 200


def test_destroy_tenant_drops_schema(db_engine):
    import uuid

    db = SessionLocal()
    slug = f"purge-{uuid.uuid4().hex[:8]}"
    try:
        set_platform_search_path(db)
        org = provision_tenant(db, slug=slug, name="Purge Me", plan_slug="operations")
        admin = AuthService.create_user(
            db, f"purge_{slug[-8:]}", f"purge_{slug[-8:]}@test.local", "TestPass123!", "Purge Admin"
        )
        AuthService.add_membership(db, admin.user_id, org.organization_id, "ADMIN", is_default=True)
        schema = org.schema_name
        org_id = org.organization_id

        with pytest.raises(ValueError):
            destroy_tenant(db, org_id, "wrong-slug")

        result = destroy_tenant(db, org_id, slug)
        assert result["deleted"] is True
        row = db.execute(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": schema},
        ).first()
        assert row is None
        assert db.query(Organization).filter(Organization.organization_id == org_id).first() is None
        assert db.query(User).filter(User.user_id == admin.user_id).first() is None
    finally:
        db.close()
