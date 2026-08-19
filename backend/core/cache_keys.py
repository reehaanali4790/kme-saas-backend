"""Tenant-scoped Redis key helpers."""
from __future__ import annotations

from core.tenant_upload import get_current_tenant_schema


def tenant_schema_for_cache() -> str:
    return get_current_tenant_schema() or "default"


def tenant_cache_key(*parts: str) -> str:
    schema = tenant_schema_for_cache()
    suffix = ":".join(str(p) for p in parts)
    return f"lme:{schema}:{suffix}"


def dashboard_key(kind: str) -> str:
    return tenant_cache_key("dashboard", kind)


def lookup_key(kind: str, query_param: str) -> str:
    return tenant_cache_key("lookup", kind, query_param)


def lookup_pattern(kind: str) -> str:
    return f"lme:{tenant_schema_for_cache()}:lookup:{kind}:*"


def dashboard_invalidate_patterns() -> list[str]:
    schema = tenant_schema_for_cache()
    return [
        f"lme:{schema}:dashboard:*",
        "lme:dashboard:*",  # legacy un-prefixed keys
    ]
