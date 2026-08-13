"""Real platform metering: document quotas, storage, AI events, API call counts.

All counters write to platform.usage_counters / platform.ai_usage_events.
No simulated values — empty UI means no real activity yet.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from config.database import SessionLocal, set_platform_search_path
from core.plan_limits import (
    check_document_limit,
    get_or_create_usage_counter,
    increment_document_usage,
)
from models.platform_models import AiUsageEvent, Organization

logger = logging.getLogger(__name__)

_metering_org_id: ContextVar[Optional[int]] = ContextVar("metering_org_id", default=None)


def set_metering_org(org_id: Optional[int]) -> None:
    _metering_org_id.set(int(org_id) if org_id else None)


def clear_metering_org() -> None:
    _metering_org_id.set(None)


def get_metering_org() -> Optional[int]:
    return _metering_org_id.get()


@contextmanager
def metering_org_scope(org_id: Optional[int]) -> Iterator[None]:
    token = _metering_org_id.set(int(org_id) if org_id else None)
    try:
        yield
    finally:
        _metering_org_id.reset(token)


def _platform_session() -> Session:
    db = SessionLocal()
    set_platform_search_path(db)
    return db


def enforce_document_quota(org_id: Optional[int] = None) -> None:
    """Raise 403 if the org is at its monthly document limit. Does not increment."""
    oid = org_id or get_metering_org()
    if not oid:
        return
    db = _platform_session()
    try:
        org = db.query(Organization).filter(Organization.organization_id == oid).first()
        if not org:
            return
        check_document_limit(db, org)
    finally:
        db.close()


def meter_document_accepted(
    org_id: Optional[int] = None,
    *,
    storage_bytes: int = 0,
    file_path: Optional[str] = None,
) -> None:
    """Increment documents_uploaded (+ storage) after a file is accepted."""
    oid = org_id or get_metering_org()
    if not oid:
        return
    size = max(0, int(storage_bytes or 0))
    if not size and file_path:
        try:
            if os.path.isfile(file_path):
                size = os.path.getsize(file_path)
        except OSError:
            size = 0

    db = _platform_session()
    try:
        org = db.query(Organization).filter(Organization.organization_id == oid).first()
        if not org:
            return
        # Soft re-check; if somehow over, still record (upload already accepted)
        try:
            check_document_limit(db, org)
        except HTTPException:
            pass
        increment_document_usage(db, oid, 1)
        if size:
            counter = get_or_create_usage_counter(db, oid)
            counter.storage_bytes = (counter.storage_bytes or 0) + size
        db.commit()
    except Exception as e:
        logger.warning("meter_document_accepted failed (ignored): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def increment_api_calls(org_id: Optional[int] = None, count: int = 1) -> None:
    oid = org_id or get_metering_org()
    if not oid:
        return
    db = _platform_session()
    try:
        counter = get_or_create_usage_counter(db, oid)
        counter.api_calls = (counter.api_calls or 0) + max(1, int(count))
        db.commit()
    except Exception as e:
        logger.warning("increment_api_calls failed (ignored): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def record_ai_usage_event(
    organization_id: Optional[int] = None,
    *,
    event_type: str = "extraction",
    model: Optional[str] = None,
    doc_type: Optional[str] = None,
    success: bool = True,
    count_api_call: bool = True,
) -> None:
    """Best-effort write to ai_usage_events (+ optional api_calls). Never breaks caller."""
    oid = organization_id if organization_id is not None else get_metering_org()
    db = _platform_session()
    try:
        db.add(
            AiUsageEvent(
                organization_id=oid,
                event_type=(event_type or "extraction")[:50],
                model=(model[:100] if model else None),
                doc_type=(doc_type[:50] if doc_type else None),
                success=bool(success),
            )
        )
        if count_api_call and oid:
            counter = get_or_create_usage_counter(db, oid)
            counter.api_calls = (counter.api_calls or 0) + 1
        db.commit()
    except Exception as e:
        logger.warning("record_ai_usage_event failed (ignored): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def meter_extraction(
    *,
    doc_type: str,
    success: bool,
    model: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> None:
    record_ai_usage_event(
        organization_id,
        event_type="extraction",
        model=model,
        doc_type=doc_type,
        success=success,
        count_api_call=True,
    )


# Back-compat for any leftover call sites
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
