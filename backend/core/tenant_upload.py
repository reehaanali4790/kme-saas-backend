"""Request-scoped tenant schema for upload path resolution."""
from __future__ import annotations

from contextvars import ContextVar, Token

_tenant_schema: ContextVar[str | None] = ContextVar("tenant_upload_schema", default=None)


def set_current_tenant_schema(schema: str | None) -> Token:
    return _tenant_schema.set(schema)


def reset_current_tenant_schema(token: Token) -> None:
    _tenant_schema.reset(token)


def get_current_tenant_schema() -> str | None:
    return _tenant_schema.get()
