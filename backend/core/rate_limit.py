"""Shared slowapi Limiter instance.

Lives in its own module (rather than main.py) so route modules can import and
apply @limiter.limit(...) without importing the FastAPI app itself.

A global default limit (settings.GLOBAL_RATE_LIMIT) applies automatically to
every route via SlowAPIMiddleware (wired in main.py) - no per-file changes
needed across the ~30 router modules. Routes that declare their own
@limiter.limit(...) (login/refresh) use that instead of the global default for
that route; it doesn't stack with it.
"""
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from config.settings import settings
from modules.auth.services import AuthService


def rate_limit_key(request: Request) -> str:
    """Key by authenticated user when possible, falling back to client IP.

    Keying purely by IP would let one office's shared/NAT'd connection burn
    through a single collective budget - this is an internal B2B tool where
    multiple employees plausibly work from the same office network. Decoding
    the JWT here is just a signature check (no DB round-trip), so it's cheap
    on every request; an invalid/missing/expired token just falls back to IP.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        payload = AuthService.decode_token(auth_header[7:])
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    return f"ip:{get_remote_address(request)}"


from core.redis import redis_cache

storage_uri = settings.REDIS_URL if (settings.REDIS_URL and redis_cache.enabled) else "memory://"
limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[settings.GLOBAL_RATE_LIMIT],
    storage_uri=storage_uri
)
