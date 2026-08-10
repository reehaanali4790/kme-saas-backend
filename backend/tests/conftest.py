"""Shared pytest fixtures — multi-tenant test database."""

import os
import pathlib
from urllib.parse import urlsplit, urlunsplit

os.environ["ENABLE_SCHEDULER"] = "false"


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base = os.environ.get("DATABASE_URL")
    if not base:
        from dotenv import load_dotenv
        load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
        base = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/lme_monitoring")

    if base.startswith("postgres://"):
        base = base.replace("postgres://", "postgresql://", 1)

    parts = urlsplit(base)
    dbname = parts.path.lstrip("/") or "lme_monitoring"
    if not dbname.endswith("_test"):
        dbname += "_test"
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


TEST_DATABASE_URL = _test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SKIP_PRODUCTION_CHECKS"] = "true"

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient


def _ensure_database_exists(url: str) -> None:
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    if not dbname.replace("_", "").isalnum():
        raise ValueError(f"Refusing to auto-create suspicious database name: {dbname!r}")

    maintenance_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": dbname}
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        maintenance_engine.dispose()


@pytest.fixture(scope="session")
def db_engine():
    _ensure_database_exists(TEST_DATABASE_URL)

    from config.database import Base, PLATFORM_SCHEMA, SHARED_SCHEMA, set_platform_search_path, set_tenant_search_path
    from models import database_models  # noqa: F401
    from models import platform_models  # noqa: F401
    from modules.tenants.provision import (
        create_platform_and_shared_tables,
        provision_default_tenant_if_missing,
    )

    engine = create_engine(TEST_DATABASE_URL)
    boot = sessionmaker(bind=engine)()
    try:
        create_platform_and_shared_tables(boot)
        org = provision_default_tenant_if_missing(boot)
        set_tenant_search_path(boot, org.schema_name)
        Base.metadata.create_all(bind=engine)
    finally:
        boot.close()

    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    connection = db_engine.connect()
    outer_transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    yield session

    session.close()
    outer_transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db_session):
    from main import app
    from core.tenant import get_tenant_db
    from config.database import get_platform_db

    def _override_tenant_db():
        from config.database import set_tenant_search_path
        from modules.tenants.provision import provision_default_tenant_if_missing

        plat = sessionmaker(bind=db_session.get_bind())()
        org = provision_default_tenant_if_missing(plat)
        set_tenant_search_path(db_session, org.schema_name)
        yield db_session

    def _override_platform_db():
        from config.database import set_platform_search_path
        set_platform_search_path(db_session)
        yield db_session

    app.dependency_overrides[get_tenant_db] = _override_tenant_db
    app.dependency_overrides[get_platform_db] = _override_platform_db
    app.state.limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


ROLE_PERMISSIONS = {
    "ADMIN": dict(can_import_lc=True, can_upload_pdf=True, can_edit_lc=True, can_delete_lc=True,
                  can_manage_users=True, can_configure_alerts=True, can_export_reports=True,
                  can_reopen_lc=True, can_change_lc_status=True),
    "MANAGER": dict(can_import_lc=True, can_upload_pdf=True, can_edit_lc=True, can_delete_lc=False,
                     can_manage_users=False, can_configure_alerts=True, can_export_reports=True,
                     can_reopen_lc=False, can_change_lc_status=True),
    "OPERATOR": dict(can_import_lc=True, can_upload_pdf=True, can_edit_lc=False, can_delete_lc=False,
                      can_manage_users=False, can_configure_alerts=False, can_export_reports=False,
                      can_reopen_lc=False, can_change_lc_status=False),
    "VIEWER": dict(can_import_lc=False, can_upload_pdf=False, can_edit_lc=False, can_delete_lc=False,
                    can_manage_users=False, can_configure_alerts=False, can_export_reports=False,
                    can_reopen_lc=False, can_change_lc_status=False),
}


@pytest.fixture()
def make_user(db_session):
    from models.database_models import Role
    from models.platform_models import Organization
    from modules.auth.services import AuthService
    from modules.tenants.provision import provision_default_tenant_if_missing
    from config.database import set_platform_search_path, set_tenant_search_path

    counter = {"n": 0}

    def _make(username=None, password="TestPass123!", role_name="ADMIN"):
        set_platform_search_path(db_session)
        org = provision_default_tenant_if_missing(db_session)
        set_tenant_search_path(db_session, org.schema_name)

        role = db_session.query(Role).filter(Role.role_name == role_name).first()
        if role is None:
            role = Role(role_name=role_name, can_view_dashboard=True, can_view_all_lcs=True,
                        **ROLE_PERMISSIONS[role_name])
            db_session.add(role)
            db_session.flush()

        counter["n"] += 1
        username = username or f"{role_name.lower()}_{counter['n']}_test"
        user = AuthService.create_user(
            db=db_session,
            username=username,
            email=f"{username}@test.local",
            password=password,
            full_name=username.title(),
        )
        AuthService.add_membership(db_session, user.user_id, org.organization_id, role_name, is_default=True)
        return user, password

    return _make


@pytest.fixture()
def authenticated_client(client, make_user):
    user, password = make_user(role_name="ADMIN")
    resp = client.post("/api/auth/login", data={"username": user.username, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
