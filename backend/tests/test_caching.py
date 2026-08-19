import re
import pytest
import orjson
from core.redis import redis_cache
from modules.reports import lookup_service as svc


class MockRedis:
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
        regex_pat = pattern.replace("*", ".*")
        regex = re.compile(f"^{regex_pat}$")
        return [k for k in self.store.keys() if regex.match(k)]

    def ping(self):
        return True


@pytest.fixture
def mock_redis_cache(monkeypatch):
    mock = MockRedis()
    monkeypatch.setattr(redis_cache, "client", mock)
    monkeypatch.setattr(redis_cache, "enabled", True)
    return mock


def test_lookup_search_cache_hit_and_miss(db_session, mock_redis_cache):
    # Ensure cache starts empty
    assert len(mock_redis_cache.store) == 0

    # 1. First search: Cache Miss -> fetches from DB -> caches result
    res1 = svc.search(db_session, "supplier", "test-q")
    assert len(mock_redis_cache.store) == 1

    # Verify cache key was created
    expected_key = "lme:default:lookup:supplier:TEST-Q"
    assert expected_key in mock_redis_cache.store

    # 2. Second search: Cache Hit -> returns cached result
    # We change the cached value manually to verify it reads from cache
    mock_redis_cache.store[expected_key] = orjson.dumps([{"id": 999, "name": "Fake Cached Supplier"}]).decode("utf-8")

    res2 = svc.search(db_session, "supplier", "test-q")
    assert len(res2) == 1
    assert res2[0]["id"] == 999
    assert res2[0]["name"] == "Fake Cached Supplier"


def test_lookup_add_invalidates_cache(db_session, mock_redis_cache):
    # Pre-populate cache with lookup keys
    mock_redis_cache.store["lme:default:lookup:supplier:abc"] = "{}"
    mock_redis_cache.store["lme:default:lookup:supplier:def"] = "{}"
    mock_redis_cache.store["lme:default:lookup:importer:xyz"] = "{}"

    # Verify supplier keys are in the cache
    assert "lme:default:lookup:supplier:abc" in mock_redis_cache.store
    assert "lme:default:lookup:importer:xyz" in mock_redis_cache.store

    # Adding a new supplier must delete all 'lme:lookup:supplier:*' keys but leave 'importer' keys
    from modules.reports.lookup_schemas import LookupCreate
    req = LookupCreate(name="New Unique Supplier")

    # Perform add
    svc.add(db_session, "supplier", req, user_id=1)

    # Assert cache invalidation
    assert "lme:default:lookup:supplier:abc" not in mock_redis_cache.store
    assert "lme:default:lookup:supplier:def" not in mock_redis_cache.store
    assert "lme:default:lookup:importer:xyz" in mock_redis_cache.store  # Importer cache must remain intact


def test_redis_graceful_degradation_when_disabled(db_session, monkeypatch):
    # Disable Redis client
    monkeypatch.setattr(redis_cache, "enabled", False)
    monkeypatch.setattr(redis_cache, "client", None)

    # Search should proceed and return successfully (bypassing Redis, querying DB directly)
    res = svc.search(db_session, "supplier", "some-query")
    assert isinstance(res, list)


@pytest.mark.asyncio
async def test_session_caching_hit_and_miss(db_session, mock_redis_cache, make_user):
    from modules.auth.dependencies import get_current_user, logout
    from models.database_models import UserSession
    from modules.auth.services import AuthService

    # 1. Create a user and an active session record in the DB
    user, _ = make_user(role_name="ADMIN")
    token = AuthService.create_access_token(data={"sub": user.user_id, "username": user.username})
    hashed_token = AuthService.hash_token(token)

    sess = UserSession(
        user_id=user.user_id,
        session_token=hashed_token,
        active=True
    )
    db_session.add(sess)
    db_session.commit()

    class MockRequest:
        headers = {"authorization": f"Bearer {token}"}
        cookies = {}
        method = "GET"
    req = MockRequest()

    cache_key = f"lme:session_active:{hashed_token}"
    assert cache_key not in mock_redis_cache.store

    # 2. First call: Cache Miss -> queries DB -> caches status "1"
    cur_user = await get_current_user(req, db_session)
    assert cur_user.user_id == user.user_id
    assert mock_redis_cache.store.get(cache_key) == "1"

    # 3. Second call: Cache Hit -> bypasses DB session check
    # We deactivate the session in the DB, but because the cache is hit, validation succeeds
    sess.active = False
    db_session.commit()

    cur_user2 = await get_current_user(req, db_session)
    assert cur_user2.user_id == user.user_id

    # 4. Logout: invalidates cache key
    class MockResponse:
        def delete_cookie(self, key, **kwargs):
            pass
    res = MockResponse()
    logout(req, res, user, db_session)
    assert cache_key not in mock_redis_cache.store


def test_dashboard_summary_cache_hit_and_miss(db_session, mock_redis_cache):
    from modules.reports import dashboard_service as dsvc

    # Ensure cache is empty
    assert "lme:default:dashboard:summary" not in mock_redis_cache.store

    # 1. First call: Cache Miss -> calculates and caches
    res1 = dsvc.summary(db_session)
    assert "lme:default:dashboard:summary" in mock_redis_cache.store

    # 2. Second call: Cache Hit
    # Modify cached value manually
    mock_redis_cache.store["lme:default:dashboard:summary"] = orjson.dumps({"fake": "summary"}).decode("utf-8")
    res2 = dsvc.summary(db_session)
    assert res2 == {"fake": "summary"}


def test_dashboard_write_invalidation_on_mutation(db_session, mock_redis_cache):
    from models.database_models import LCMaster
    from modules.reports import dashboard_service as dsvc

    # Pre-populate dashboard cache
    mock_redis_cache.store["lme:default:dashboard:summary"] = "{}"
    mock_redis_cache.store["lme:default:dashboard:arrivals"] = "{}"

    # Verify keys exist in cache
    assert "lme:default:dashboard:summary" in mock_redis_cache.store
    assert "lme:default:dashboard:arrivals" in mock_redis_cache.store

    from datetime import date
    # Perform database insert on LCMaster (triggering lifecycle listener)
    lc = LCMaster(
        lc_number="LC-TEST-CACHE-1",
        lc_date=date.today(),
        monitoring_expiry=date.today(),
        status="OPEN"
    )
    db_session.add(lc)
    db_session.commit()

    # Verify keys were deleted from cache!
    assert "lme:default:dashboard:summary" not in mock_redis_cache.store
    assert "lme:default:dashboard:arrivals" not in mock_redis_cache.store


