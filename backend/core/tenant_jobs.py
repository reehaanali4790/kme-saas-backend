"""Per-tenant background job runner."""

import logging
from typing import Callable

from config.database import SessionLocal, set_platform_search_path, set_tenant_search_path
from core.tenant import list_active_tenant_contexts, TenantContext
from models.platform_models import Organization

logger = logging.getLogger(__name__)


def run_for_all_tenants(job_name: str, fn: Callable[[TenantContext], None]) -> dict:
    platform_db = SessionLocal()
    results = {"job": job_name, "tenants": 0, "errors": []}
    try:
        set_platform_search_path(platform_db)
        contexts = list_active_tenant_contexts(platform_db)
        results["tenants"] = len(contexts)
        for ctx in contexts:
            tenant_db = SessionLocal()
            try:
                set_tenant_search_path(tenant_db, ctx.schema_name)
                fn(ctx, tenant_db)
                tenant_db.commit()
            except Exception as e:
                logger.error("[%s] tenant %s failed: %s", job_name, ctx.schema_name, e, exc_info=True)
                results["errors"].append({"tenant": ctx.schema_name, "error": str(e)})
                tenant_db.rollback()
            finally:
                tenant_db.close()
    finally:
        platform_db.close()
    return results
