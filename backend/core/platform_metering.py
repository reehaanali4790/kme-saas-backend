"""Helpers to meter document / AI usage against platform.usage_counters."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from config.database import SessionLocal, set_platform_search_path
from core.plan_limits import (
    check_document_limit,
    get_or_create_usage_counter,
    increment_document_usage,
)
from models.platform_models import AiUsageEvent, Organization

logger = logging.getLogger(__name__)


def enforce_and_increment_document(
    platform_db: Session,
    org_id: int,
    *,
    storage_bytes: int = 0,
) -> None:
    org = platform_db.query(Organization).filter(Organization.organization_id == org_id).first()
    if not org:
        return
    check_document_limit(platform_db, org)
    increment_document_usage(platform_db, org_id, 1)
    if storage_bytes:
        counter = get_or_create_usage_counter(platform_db, org_id)
        counter.storage_bytes = (counter.storage_bytes or 0) + max(0, storage_bytes)
    platform_db.flush()


def record_ai_usage_event(
    organization_id: Optional[int],
    *,
    event_type: str = "extraction",
    model: Optional[str] = None,
    doc_type: Optional[str] = None,
    success: bool = True,
) -> None:
    """Best-effort write; never breaks the calling request."""
    db = SessionLocal()
    try:
        set_platform_search_path(db)
        db.add(
            AiUsageEvent(
                organization_id=organization_id,
                event_type=event_type[:50],
                model=(model[:100] if model else None),
                doc_type=(doc_type[:50] if doc_type else None),
                success=success,
            )
        )
        db.commit()
    except Exception as e:
        logger.warning("record_ai_usage_event failed (ignored): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
