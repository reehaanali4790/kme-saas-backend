"""Password hashing and JWT encode/decode primitives.

Uses PyJWT + bcrypt directly rather than python-jose + passlib: passlib is
unmaintained (last release ~2020) and needed a bcrypt.hashpw monkeypatch
(previously in services/auth_service.py) just to work with modern bcrypt
(>=4.0) at all; python-jose has a history of algorithm-confusion CVEs in the
wider "jose" ecosystem. Protected by the auth test suite (tests/test_auth.py)
- existing password hashes and already-issued tokens both remain valid across
this swap: bcrypt hash strings are a standard format regardless of which
library produced them, and HS256 JWTs are byte-for-byte reproducible given the
same header/payload/secret, so nothing already stored needs migrating.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import jwt

from config.settings import settings

# bcrypt's algorithm only uses the first 72 bytes of the password; truncate
# explicitly (matching the previous passlib-based behavior) instead of letting
# the bcrypt library raise ValueError on longer input.
_MAX_BCRYPT_BYTES = 72


def _bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_BCRYPT_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_bytes(plain_password), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _encode(data: dict, expire: datetime, token_type: str) -> str:
    to_encode = dict(data)
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    # jti makes every token unique even when minted for the same user in the same
    # second (same sub/exp/type would otherwise produce a byte-identical JWT,
    # which matters because UserSession.session_token stores a hash of it under
    # a unique constraint - see tests/test_auth.py::test_refresh_issues_a_working_token).
    to_encode.update({"exp": expire, "type": token_type, "jti": secrets.token_urlsafe(16)})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return _encode(data, expire, "access")


def create_refresh_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return _encode(data, expire, "refresh")


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return None
