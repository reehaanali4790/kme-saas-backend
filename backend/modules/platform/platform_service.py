"""Platform control-plane queries for the SaaS Admin Suite."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from config.settings import settings
from core.plan_limits import get_organization_plan
from core.redis import redis_cache
from models.database_models import LMESyncRun
from models.platform_models import (
    AiUsageEvent,
    Organization,
    OrganizationMembership,
    Plan,
    PlatformAuditLog,
    Subscription,
    UsageCounter,
    User,
)


def _member_counts(db: Session) -> dict[int, int]:
    rows = (
        db.query(
            OrganizationMembership.organization_id,
            func.count(OrganizationMembership.membership_id),
        )
        .group_by(OrganizationMembership.organization_id)
        .all()
    )
    return {int(oid): int(cnt) for oid, cnt in rows}


def _usage_status(docs_used: int, docs_max: Optional[int], users_used: int, users_max: Optional[int]) -> str:
    over = False
    warn = False
    if docs_max is not None:
        if docs_used >= docs_max:
            over = True
        elif docs_used >= docs_max * 0.8:
            warn = True
    if users_max is not None:
        if users_used >= users_max:
            over = True
        elif users_used >= users_max * 0.8:
            warn = True
    if over:
        return "over"
    if warn:
        return "warning"
    return "ok"


def build_usage_rows(db: Session) -> list[dict[str, Any]]:
    orgs = db.query(Organization).order_by(Organization.name).all()
    counts = _member_counts(db)
    period_start = date.today().replace(day=1)
    counters = {
        c.organization_id: c
        for c in db.query(UsageCounter).filter(UsageCounter.period_start == period_start).all()
    }
    rows = []
    for o in orgs:
        plan = get_organization_plan(db, o)
        counter = counters.get(o.organization_id)
        docs_used = int(counter.documents_uploaded or 0) if counter else 0
        storage = int(counter.storage_bytes or 0) if counter else 0
        api_calls = int(counter.api_calls or 0) if counter else 0
        users_used = counts.get(o.organization_id, 0)
        docs_max = plan.max_documents_per_month if plan else None
        users_max = plan.max_users if plan else None
        rows.append(
            {
                "organization_id": o.organization_id,
                "name": o.name,
                "slug": o.slug,
                "status": o.status,
                "plan": plan.slug if plan else None,
                "plan_name": plan.name if plan else None,
                "users_used": users_used,
                "users_max": users_max,
                "documents_used": docs_used,
                "documents_max": docs_max,
                "storage_bytes": storage,
                "api_calls": api_calls,
                "period_start": period_start.isoformat(),
                "usage_status": _usage_status(docs_used, docs_max, users_used, users_max),
            }
        )
    return rows


def build_usage_for_org(db: Session, org_id: int) -> dict[str, Any]:
    org = db.query(Organization).filter(Organization.organization_id == org_id).first()
    if not org:
        raise ValueError("Organization not found")
    rows = [r for r in build_usage_rows(db) if r["organization_id"] == org_id]
    return rows[0] if rows else {}


def build_billing_rows(db: Session) -> list[dict[str, Any]]:
    orgs = db.query(Organization).order_by(Organization.name).all()
    # Keep latest sub per org
    latest: dict[int, Subscription] = {}
    for s in db.query(Subscription).order_by(Subscription.created_at.desc()).all():
        if s.organization_id not in latest:
            latest[s.organization_id] = s

    now = datetime.utcnow()
    rows = []
    for o in orgs:
        plan = get_organization_plan(db, o)
        sub = latest.get(o.organization_id)
        trial_ends = o.trial_ends_at
        days_to_trial = None
        if trial_ends:
            days_to_trial = (trial_ends - now).days
        cust = o.stripe_customer_id or ""
        masked = (cust[:8] + "…") if len(cust) > 8 else (cust or None)
        rows.append(
            {
                "organization_id": o.organization_id,
                "name": o.name,
                "slug": o.slug,
                "org_status": o.status,
                "plan": plan.slug if plan else None,
                "plan_name": plan.name if plan else None,
                "subscription_status": sub.status if sub else None,
                "billing_period": sub.billing_period if sub else None,
                "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
                "days_to_trial_end": days_to_trial,
                "stripe_customer_id_masked": masked,
                "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
            }
        )
    return rows


def build_plans(db: Session) -> list[dict[str, Any]]:
    plans = db.query(Plan).order_by(Plan.plan_id).all()
    return [
        {
            "plan_id": p.plan_id,
            "slug": p.slug,
            "name": p.name,
            "max_users": p.max_users,
            "max_documents_per_month": p.max_documents_per_month,
            "price_monthly": float(p.price_monthly) if p.price_monthly is not None else None,
            "price_annual": float(p.price_annual) if p.price_annual is not None else None,
            "feature_flags": p.feature_flags or {},
            "active": bool(p.active),
        }
        for p in plans
    ]


def update_plan(db: Session, plan_id: int, body: dict) -> dict[str, Any]:
    plan = db.query(Plan).filter(Plan.plan_id == plan_id).first()
    if not plan:
        raise ValueError("Plan not found")
    if "name" in body and body["name"]:
        plan.name = str(body["name"])[:100]
    if "max_users" in body:
        plan.max_users = body["max_users"]
    if "max_documents_per_month" in body:
        plan.max_documents_per_month = body["max_documents_per_month"]
    if "feature_flags" in body and isinstance(body["feature_flags"], dict):
        plan.feature_flags = body["feature_flags"]
    if "active" in body:
        plan.active = bool(body["active"])
    db.commit()
    db.refresh(plan)
    return build_plans(db)  # return catalog; caller can filter


def build_audit(db: Session, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    q = db.query(PlatformAuditLog).order_by(PlatformAuditLog.created_at.desc())
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    user_ids = {r.user_id for r in rows if r.user_id}
    users = {
        u.user_id: u
        for u in db.query(User).filter(User.user_id.in_(user_ids)).all()
    } if user_ids else {}
    org_ids = {r.organization_id for r in rows if r.organization_id}
    orgs = {
        o.organization_id: o
        for o in db.query(Organization).filter(Organization.organization_id.in_(org_ids)).all()
    } if org_ids else {}

    items = []
    for r in rows:
        u = users.get(r.user_id) if r.user_id else None
        o = orgs.get(r.organization_id) if r.organization_id else None
        items.append(
            {
                "log_id": r.log_id,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "description": r.description,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "actor_username": u.username if u else None,
                "actor_email": u.email if u else None,
                "organization_id": r.organization_id,
                "organization_name": o.name if o else None,
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def _upload_dir_size_bytes() -> Optional[int]:
    root = settings.UPLOAD_DIR
    if not root or not os.path.isdir(root):
        return None
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
    except OSError:
        return None
    return total


def build_infra(db: Session, app_state: Any = None) -> dict[str, Any]:
    redis_ok = False
    try:
        redis_ok = bool(redis_cache.enabled and redis_cache.client and redis_cache.client.ping())
    except Exception:
        redis_ok = False

    last_sync = db.query(LMESyncRun).order_by(LMESyncRun.started_at.desc()).first()
    scheduler_jobs = []
    scheduler = getattr(app_state, "scheduler", None) if app_state else None
    if scheduler is not None:
        try:
            for job in scheduler.get_jobs():
                scheduler_jobs.append(
                    {
                        "id": job.id,
                        "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                    }
                )
        except Exception:
            pass

    return {
        "environment": settings.ENVIRONMENT,
        "enable_scheduler": bool(settings.ENABLE_SCHEDULER),
        "scheduler_jobs": scheduler_jobs,
        "redis_connected": redis_ok,
        "redis_configured": bool(settings.REDIS_URL),
        "upload_dir": settings.UPLOAD_DIR,
        "upload_dir_size_bytes": _upload_dir_size_bytes(),
        "max_upload_size": settings.MAX_UPLOAD_SIZE,
        "kpt_eta_enabled": bool(settings.KPT_ETA_CRAWLER_ENABLED),
        "kpt_on_port_enabled": bool(settings.KPT_ON_PORT_CRAWLER_ENABLED),
        "kpt_departures_enabled": bool(getattr(settings, "KPT_DEPARTURES_CRAWLER_ENABLED", False)),
        "alert_scan_enabled": bool(settings.ALERT_SCAN_ENABLED),
        "lme_web_sync_enabled": bool(settings.LME_WEB_SYNC_ENABLED),
        "lme_bulletin_crawler_enabled": bool(settings.LME_BULLETIN_CRAWLER_ENABLED),
        "last_lme_sync": {
            "status": last_sync.status if last_sync else None,
            "started_at": last_sync.started_at.isoformat() if last_sync and last_sync.started_at else None,
            "finished_at": last_sync.finished_at.isoformat() if last_sync and last_sync.finished_at else None,
            "trigger": last_sync.trigger if last_sync else None,
            "error_message": last_sync.error_message if last_sync else None,
        },
    }


def build_ai_overview(db: Session) -> dict[str, Any]:
    period_start = date.today().replace(day=1)
    period_dt = datetime.combine(period_start, datetime.min.time())
    q = db.query(AiUsageEvent).filter(AiUsageEvent.created_at >= period_dt)
    total = q.count()
    success = q.filter(AiUsageEvent.success == True).count()  # noqa: E712
    by_org_rows = (
        db.query(AiUsageEvent.organization_id, func.count(AiUsageEvent.event_id))
        .filter(AiUsageEvent.created_at >= period_dt)
        .group_by(AiUsageEvent.organization_id)
        .all()
    )
    by_doc_rows = (
        db.query(AiUsageEvent.doc_type, func.count(AiUsageEvent.event_id))
        .filter(AiUsageEvent.created_at >= period_dt)
        .group_by(AiUsageEvent.doc_type)
        .all()
    )
    by_event_rows = (
        db.query(AiUsageEvent.event_type, func.count(AiUsageEvent.event_id))
        .filter(AiUsageEvent.created_at >= period_dt)
        .group_by(AiUsageEvent.event_type)
        .all()
    )
    org_ids = [oid for oid, _ in by_org_rows if oid]
    orgs = {
        o.organization_id: o
        for o in db.query(Organization).filter(Organization.organization_id.in_(org_ids)).all()
    } if org_ids else {}
    by_org = [
        {
            "organization_id": oid,
            "name": orgs[oid].name if oid in orgs else None,
            "slug": orgs[oid].slug if oid in orgs else None,
            "events": int(cnt),
        }
        for oid, cnt in by_org_rows
    ]
    by_org.sort(key=lambda x: x["events"], reverse=True)

    by_doc_type = [
        {"doc_type": dt or "unknown", "events": int(cnt)}
        for dt, cnt in by_doc_rows
    ]
    by_doc_type.sort(key=lambda x: x["events"], reverse=True)

    by_event_type = [
        {"event_type": et or "unknown", "events": int(cnt)}
        for et, cnt in by_event_rows
    ]
    by_event_type.sort(key=lambda x: x["events"], reverse=True)

    return {
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "gemini_model": settings.GEMINI_MODEL,
        "extraction_gemini_enabled": bool(settings.EXTRACTION_GEMINI_ENABLED),
        "extraction_claude_fallback": bool(settings.EXTRACTION_CLAUDE_FALLBACK),
        "period_start": period_start.isoformat(),
        "events_this_month": total,
        "success_this_month": success,
        "failed_this_month": total - success,
        "by_organization": by_org,
        "by_doc_type": by_doc_type,
        "by_event_type": by_event_type,
    }


def build_settings_health() -> dict[str, Any]:
    return {
        "environment": settings.ENVIRONMENT,
        "debug": bool(settings.DEBUG),
        "allowed_origins_set": bool(settings.ALLOWED_ORIGINS and settings.ALLOWED_ORIGINS != "*"),
        "app_public_url_set": bool(settings.APP_PUBLIC_URL),
        "secret_key_set": bool(settings.SECRET_KEY and len(settings.SECRET_KEY) >= 32),
        "database_configured": bool(settings.DATABASE_URL),
        "redis_configured": bool(settings.REDIS_URL),
        "stripe_configured": bool(settings.STRIPE_SECRET_KEY),
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "scheduler_enabled": bool(settings.ENABLE_SCHEDULER),
        "upload_dir": settings.UPLOAD_DIR,
    }


def build_command_center(db: Session, app_state: Any = None) -> dict[str, Any]:
    orgs = db.query(Organization).all()
    by_status: dict[str, int] = {}
    for o in orgs:
        by_status[o.status] = by_status.get(o.status, 0) + 1

    usage_rows = build_usage_rows(db)
    over_quota = [r for r in usage_rows if r["usage_status"] == "over"]
    warning_quota = [r for r in usage_rows if r["usage_status"] == "warning"]
    docs_month = sum(r["documents_used"] for r in usage_rows)

    now = datetime.utcnow()
    soon = now + timedelta(days=7)
    trials_expiring = [
        {
            "organization_id": o.organization_id,
            "name": o.name,
            "trial_ends_at": o.trial_ends_at.isoformat(),
        }
        for o in orgs
        if o.trial_ends_at and now <= o.trial_ends_at <= soon
    ]

    alerts = []
    if by_status.get("suspended"):
        alerts.append({"severity": "warning", "code": "suspended", "message": f"{by_status['suspended']} suspended companies"})
    if over_quota:
        alerts.append({"severity": "critical", "code": "over_quota", "message": f"{len(over_quota)} companies over plan quota"})
    if warning_quota:
        alerts.append({"severity": "warning", "code": "near_quota", "message": f"{len(warning_quota)} companies near plan quota"})
    if trials_expiring:
        alerts.append({"severity": "warning", "code": "trial_ending", "message": f"{len(trials_expiring)} trials ending within 7 days"})
    if not settings.GEMINI_API_KEY and not settings.ANTHROPIC_API_KEY:
        alerts.append({"severity": "critical", "code": "ai_keys", "message": "No AI API keys configured"})
    elif not settings.GEMINI_API_KEY or not settings.ANTHROPIC_API_KEY:
        alerts.append({"severity": "info", "code": "ai_keys_partial", "message": "Only one AI provider key is configured"})
    if not settings.ENABLE_SCHEDULER:
        alerts.append({"severity": "warning", "code": "scheduler_off", "message": "Background scheduler is disabled on this instance"})

    audit = build_audit(db, page=1, page_size=8)
    recent_orgs = sorted(orgs, key=lambda o: o.created_at or datetime.min, reverse=True)[:5]

    return {
        "kpis": {
            "organizations_total": len(orgs),
            "organizations_active": by_status.get("active", 0),
            "organizations_by_status": by_status,
            "users_total": db.query(func.count(User.user_id)).scalar() or 0,
            "documents_this_month": docs_month,
            "over_quota_count": len(over_quota),
        },
        "alerts": alerts,
        "trials_expiring": trials_expiring,
        "over_quota": over_quota[:10],
        "recent_audit": audit["items"],
        "recent_organizations": [
            {
                "organization_id": o.organization_id,
                "name": o.name,
                "slug": o.slug,
                "status": o.status,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in recent_orgs
        ],
        "ai": {
            "gemini_configured": bool(settings.GEMINI_API_KEY),
            "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        },
        "infra_snapshot": {
            "enable_scheduler": bool(settings.ENABLE_SCHEDULER),
            "redis_configured": bool(settings.REDIS_URL),
        },
    }
