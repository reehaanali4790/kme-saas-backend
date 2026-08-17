"""
LME Monitoring System - Main Application (UPDATED)
Version: 2.0
Entry point for the FastAPI application with all API endpoints
"""
from typing import cast
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.orm import Session
import sys
import os
import logging

# Add backend to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import settings
from config.database import engine, Base, get_db, get_platform_db, check_db_connection, PLATFORM_SCHEMA, SHARED_SCHEMA
from schema_guard import ensure_runtime_schema
from core.exceptions import AppError
from core.logging import configure_logging
from core.rate_limit import limiter
from models import database_models
from models import platform_models  # noqa: F401 — register platform schema tables

configure_logging()
logger = logging.getLogger("uvicorn")

if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        )
        logger.info("Sentry initialized")
    except ImportError:
        logger.warning("SENTRY_DSN set but sentry-sdk not installed")

# Import the single aggregated API router (see modules/api.py — add new module
# routers there, not here).
from modules.api import api_router

# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-Based LME Price Monitoring System with WhatsApp Alerts",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# Unhandled-exception safety net — MUST be added before CORSMiddleware.
# Starlette pulls any handler registered for the base `Exception` class (see
# general_exception_handler below) out of the normal exception-handler chain and
# runs it inside ServerErrorMiddleware, which wraps EVERY user middleware —
# including CORSMiddleware. So a route that raises something other than
# HTTPException/AppError/RequestValidationError/DataError/SQLAlchemyError never
# gets a CORS header on its 500 response, and the browser reports a misleading
# "blocked by CORS policy" error instead of the real 500. Middleware added
# earlier ends up closer to the route (see Starlette's add_middleware, which
# inserts at position 0), so registering this ahead of CORSMiddleware puts it
# between CORS and the route — it catches the exception and returns a normal
# response that CORSMiddleware still gets to add headers to.
@app.middleware("http")
async def catch_unhandled_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.error(f"Unhandled error on {request.method} {request.url.path}: "
                     f"{type(exc).__name__}: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong on the server. Please try again.",
                     "error": True}
        )

# CORS Middleware
# A wildcard origin ("*") combined with allow_credentials=True is an invalid/
# insecure combination — browsers reject credentialed requests against a
# wildcard-origin response. If ALLOWED_ORIGINS is still "*" (dev default),
# credentials are disabled instead of silently emitting a broken CORS policy;
# production must set ALLOWED_ORIGINS to explicit origins (enforced at startup
# by Settings._validate_production_safety) to get credentialed CORS at all.
_wildcard_origins = "*" in settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=not _wildcard_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request size limit — rejects any request declaring a body larger than
# MAX_UPLOAD_SIZE before it's read, so oversized file uploads (or any other
# oversized body) can't fill disk/memory. Every current upload endpoint streams
# straight to disk with no per-route size check of its own, so this is the one
# place that actually enforces the setting.
@app.middleware("http")
async def max_body_size_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > settings.MAX_UPLOAD_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large", "error": True},
                )
        except ValueError:
            pass
    return await call_next(request)

# Secure HTTP Headers Middleware
@app.middleware("http")
async def secure_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    
    # 1. HSTS (Strict-Transport-Security) - only in production
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        
    # 2. Prevent Clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    
    # 3. MIME-Type Sniffing Protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # 4. Referrer Policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    # 5. Content Security Policy (CSP)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob: https://*; "
        "connect-src 'self' ws: wss: http://localhost:8000; "
        "frame-ancestors 'none';"
    )
    
    return response

# Rate limiting (slowapi) — see core/rate_limit.py and the @limiter.limit(...)
# decorators on the auth login/refresh endpoints.
app.state.limiter = limiter

# Starlette's ExceptionHandler protocol requires the second parameter to be
# typed as `Exception` (not a subclass). slowapi types _rate_limit_exceeded_handler
# with the narrower `RateLimitExceeded`, which Pyright correctly rejects due to
# contravariance. The wrapper below satisfies the protocol; cast() narrows the
# type internally with zero runtime overhead.
def _typed_rate_limit_handler(request: Request, exc: Exception) -> Response:
    return _rate_limit_exceeded_handler(request, cast(RateLimitExceeded, exc))

app.add_exception_handler(RateLimitExceeded, _typed_rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

# Include API routers — single aggregated router, see modules/api.py
app.include_router(api_router)

def _nbp_daily_job():
    """Scheduled daily fetch of NBP currency rates."""
    from config.database import SessionLocal
    from integrations.nbp.nbp_rate_service import fetch_nbp_rates
    from modules.currency_rates.router import upsert_nbp_rate
    db = SessionLocal()
    try:
        data = fetch_nbp_rates()
        res = upsert_nbp_rate(db, data)
        logger.info(f"[NBP scheduler] {res['action']} rates for {res['rate_date']}: "
                    f"USD={res['usd_rate']} EUR={res['eur_rate']} AED={res.get('aed_rate')}")
    except Exception:
        logger.error("[NBP scheduler] failed", exc_info=True)
    finally:
        db.close()


def _kpt_daily_job():
    """Scheduled daily KPT crawl: Expected Arrivals (ETA) then Ships On Port — per tenant."""
    if not settings.KPT_ETA_CRAWLER_ENABLED and not settings.KPT_ON_PORT_CRAWLER_ENABLED:
        return
    from core.tenant_jobs import run_for_all_tenants

    def _run(ctx, tenant_db):
        if settings.KPT_ETA_CRAWLER_ENABLED:
            from integrations.kpt.kpt_eta_sync import run_kpt_eta_sync
            res = run_kpt_eta_sync(
                tenant_db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_ETA_URL,
            )
            logger.info(
                "[%s KPT ETA] eligible=%s matched=%s updated=%s not_found=%s",
                ctx.schema_name, res["eligible_vessels"], res["matched"],
                res["updated_shipments"], len(res["not_found"]),
            )
        if settings.KPT_ON_PORT_CRAWLER_ENABLED:
            from integrations.kpt.kpt_on_port_sync import run_kpt_on_port_sync
            res2 = run_kpt_on_port_sync(
                tenant_db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_ON_PORT_URL,
            )
            logger.info(
                "[%s KPT on-port] eligible=%s matched=%s updated=%s not_found=%s",
                ctx.schema_name, res2["eligible_vessels"], res2["matched"],
                res2["updated_shipments"], len(res2["not_found"]),
            )
        if settings.KPT_DOC_ALERTS_ENABLED:
            from integrations.kpt.kpt_document_alerts import run_kpt_document_alert_scan
            alerts = run_kpt_document_alert_scan(tenant_db)
            logger.info(
                "[%s KPT doc alerts] created=%s resolved=%s",
                ctx.schema_name, alerts["total_created"], alerts["total_resolved"],
            )

    result = run_for_all_tenants("kpt_daily", _run)
    logger.info("[KPT daily scheduler] tenants=%s errors=%s", result["tenants"], len(result["errors"]))


def _alert_scan_job():
    """Per-tenant operational alert engine scan."""
    from core.tenant_jobs import run_for_all_tenants
    from modules.alerts.engine_service import run_scan

    def _run(ctx, tenant_db):
        run_scan(tenant_db, send_whatsapp=True)

    result = run_for_all_tenants("alert_scan", _run)
    logger.info("[Alert scan scheduler] tenants=%s errors=%s", result["tenants"], len(result["errors"]))


def _lme_web_sync_job():
    """Scheduled auto-sync of the thehelpers.pk LME bulletin (source='WEB'). Manages
    its own DB session and never raises - sync_web_bulletin() logs an LMESyncRun row
    for every attempt, success or failure."""
    from integrations.thehelpers.thehelpers_lme_service import sync_web_bulletin
    result = sync_web_bulletin(trigger='CRON')
    logger.info(f"[LME web-sync scheduler] {result.get('status')} - {result}")


def _lme_bulletin_crawler_job():
    """Scheduled auto-crawl of thehelpers.pk bulletins into the manual-upload pipeline
    (source='MANUAL', feeds the Rate Matrix/cards directly). Separate job from the WEB
    sync above - manages its own DB session and never raises; logs an
    LMEBulletinCrawlLog row per bulletin candidate it considers."""
    from integrations.thehelpers.thehelpers_bulletin_crawler import crawl_and_import_bulletins
    result = crawl_and_import_bulletins(trigger='CRON')
    logger.info(f"[LME bulletin crawler] {result}")


def _kpt_port_cycle_job():
    """Every N hours: Ships On Port then Ship Departures — per tenant."""
    if not settings.KPT_ON_PORT_CRAWLER_ENABLED and not settings.KPT_DEPARTURES_CRAWLER_ENABLED:
        return
    from core.tenant_jobs import run_for_all_tenants

    def _run(ctx, tenant_db):
        if settings.KPT_ON_PORT_CRAWLER_ENABLED:
            from integrations.kpt.kpt_on_port_sync import run_kpt_on_port_sync
            res = run_kpt_on_port_sync(
                tenant_db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_ON_PORT_URL,
            )
            logger.info(
                "[%s KPT port cycle on-port] eligible=%s matched=%s updated=%s",
                ctx.schema_name, res["eligible_vessels"], res["matched"], res["updated_shipments"],
            )
        if settings.KPT_DEPARTURES_CRAWLER_ENABLED:
            from integrations.kpt.kpt_departures_sync import run_kpt_departures_sync
            res2 = run_kpt_departures_sync(
                tenant_db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_DEPARTURES_URL,
            )
            logger.info(
                "[%s KPT port cycle departures] kpt_rows=%s matched=%s updated=%s",
                ctx.schema_name, res2["kpt_rows"], res2["matched"], res2["updated_shipments"],
            )
        if settings.KPT_DOC_ALERTS_ENABLED:
            from integrations.kpt.kpt_document_alerts import run_kpt_document_alert_scan
            alerts = run_kpt_document_alert_scan(tenant_db)
            logger.info(
                "[%s KPT port cycle doc alerts] created=%s resolved=%s",
                ctx.schema_name, alerts["total_created"], alerts["total_resolved"],
            )

    result = run_for_all_tenants("kpt_port_cycle", _run)
    logger.info("[KPT port cycle scheduler] tenants=%s errors=%s", result["tenants"], len(result["errors"]))


# Startup event
@app.on_event("startup")
async def startup_event():
    """Run on application startup"""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION} (environment={settings.ENVIRONMENT})")

    # Check database connection
    if check_db_connection():
        logger.info("Database connection successful")
    else:
        logger.error("Database connection failed - check DATABASE_URL in settings")
        return

    # Bootstrap multi-tenant schemas
    try:
        from config.database import SessionLocal
        from modules.tenants.provision import provision_default_tenant_if_missing, create_platform_and_shared_tables

        boot_db = SessionLocal()
        try:
            create_platform_and_shared_tables(boot_db)
            org = provision_default_tenant_if_missing(boot_db)
            if org:
                logger.info("Default tenant ready: %s (%s)", org.slug, org.schema_name)
            from modules.platform.bootstrap import ensure_platform_owner_user, sync_platform_admin_emails
            owner = ensure_platform_owner_user(boot_db, org)
            if owner:
                logger.info("Platform owner account ready: %s", owner.username)
            promoted = sync_platform_admin_emails(boot_db)
            if promoted:
                logger.info("Promoted %d platform admin(s) from PLATFORM_ADMIN_EMAILS", promoted)
        finally:
            boot_db.close()
    except Exception:
        logger.error("Tenant bootstrap failed", exc_info=True)

    # Create tables if they don't exist (legacy fallback)
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables ready")
    except Exception:
        logger.error("Error creating tables", exc_info=True)

    # Additive-only column patches for ORM columns added after the last migration —
    # self-heals if a deploy skips its migration step (see schema_guard.py).
    try:
        ensure_runtime_schema()
    except Exception:
        logger.error("Runtime schema guard failed", exc_info=True)

    # Daily NBP currency-rate auto-fetch (08:00 Pakistan Standard Time).
    # Use a FIXED UTC+5 offset rather than the "Asia/Karachi" string: APScheduler 3.11
    # resolves string timezones via zoneinfo.ZoneInfo, which needs a tz database that the
    # minimal Railway image lacks (no system zoneinfo + tzdata not guaranteed) — that made
    # the scheduler raise ZoneInfoNotFoundError and silently never start in production.
    # Pakistan has no daylight saving, so a constant +5 offset is always correct.
    #
    # ENABLE_SCHEDULER guards all of this (jobs + the catch-up thread below): these run
    # in-process, so more than one web instance running them concurrently would fire
    # duplicate crons and duplicate catch-up fetches. Keep this True on exactly one
    # instance if the web tier is ever scaled horizontally.
    if not settings.ENABLE_SCHEDULER:
        logger.info("Scheduler disabled (ENABLE_SCHEDULER=false) - skipping cron jobs on this instance")
    else:
        try:
            from datetime import timezone as _tz, timedelta as _td
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            PKT = _tz(_td(hours=5))
            scheduler = BackgroundScheduler(timezone=PKT)
            nbp_job = scheduler.add_job(_nbp_daily_job, 'cron', hour=8, minute=0, id='nbp_daily',
                                        misfire_grace_time=3600, coalesce=True)
            lme_web_sync_job = None
            if settings.LME_WEB_SYNC_ENABLED:
                lme_web_sync_job = scheduler.add_job(
                    _lme_web_sync_job,
                    CronTrigger.from_crontab(settings.LME_WEB_SYNC_CRON, timezone=PKT),
                    id='lme_web_sync',
                    misfire_grace_time=3600,
                    coalesce=True,
                    max_instances=1,
                )
            lme_bulletin_crawler_job = None
            if settings.LME_BULLETIN_CRAWLER_ENABLED:
                lme_bulletin_crawler_job = scheduler.add_job(
                    _lme_bulletin_crawler_job,
                    CronTrigger.from_crontab(settings.LME_BULLETIN_CRAWLER_CRON, timezone=PKT),
                    id='lme_bulletin_crawler',
                    misfire_grace_time=3600,
                    coalesce=True,
                    max_instances=1,
                )
            kpt_daily_job = None
            if settings.KPT_ETA_CRAWLER_ENABLED or settings.KPT_ON_PORT_CRAWLER_ENABLED:
                kpt_daily_job = scheduler.add_job(
                    _kpt_daily_job,
                    'cron',
                    hour=settings.KPT_ETA_SCHEDULE_HOUR,
                    minute=settings.KPT_ETA_SCHEDULE_MINUTE,
                    id='kpt_daily',
                    misfire_grace_time=3600,
                    coalesce=True,
                )
            alert_scan_job = None
            if settings.ALERT_SCAN_ENABLED:
                alert_scan_job = scheduler.add_job(
                    _alert_scan_job,
                    'interval',
                    minutes=settings.ALERT_SCAN_INTERVAL_MINUTES,
                    id='alert_scan',
                    max_instances=1,       # never let two scans overlap
                    coalesce=True,         # a missed run doesn't queue up backlog runs
                    misfire_grace_time=300,
                )
            kpt_port_cycle_job = None
            if settings.KPT_ON_PORT_CRAWLER_ENABLED or settings.KPT_DEPARTURES_CRAWLER_ENABLED:
                kpt_port_cycle_job = scheduler.add_job(
                    _kpt_port_cycle_job,
                    'interval',
                    hours=settings.KPT_PORT_CYCLE_HOURS,
                    id='kpt_port_cycle',
                    misfire_grace_time=1800,
                    coalesce=True,
                )
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info(f"NBP daily rate scheduler started (08:00 PKT / UTC+5). Next run: {nbp_job.next_run_time}")
            if alert_scan_job is not None:
                logger.info(
                    f"Alert scan scheduler started (every {settings.ALERT_SCAN_INTERVAL_MINUTES}m). "
                    f"Next run: {alert_scan_job.next_run_time}"
                )
            if kpt_daily_job is not None:
                logger.info(
                    f"KPT port sync scheduler started "
                    f"({settings.KPT_ETA_SCHEDULE_HOUR:02d}:{settings.KPT_ETA_SCHEDULE_MINUTE:02d} PKT). "
                    f"Next run: {kpt_daily_job.next_run_time}"
                )
            if kpt_port_cycle_job is not None:
                logger.info(
                    f"KPT on-port + departures cycle started "
                    f"(every {settings.KPT_PORT_CYCLE_HOURS}h). Next run: {kpt_port_cycle_job.next_run_time}"
                )
            if lme_web_sync_job is not None:
                logger.info(
                    f"LME web-sync scheduler started (cron: {settings.LME_WEB_SYNC_CRON} PKT). "
                    f"Next run: {lme_web_sync_job.next_run_time}"
                )
            if lme_bulletin_crawler_job is not None:
                logger.info(
                    f"LME bulletin crawler started (cron: {settings.LME_BULLETIN_CRAWLER_CRON} PKT). "
                    f"Next run: {lme_bulletin_crawler_job.next_run_time}"
                )
        except Exception:
            logger.error("Scheduler not started", exc_info=True)

        # Catch-up: if today's rate is missing (missed schedule / fresh deploy / restart),
        # fetch it once in a background thread so prod self-heals instead of waiting for 08:00.
        try:
            import threading

            def _catch_up():
                from datetime import date
                from config.database import SessionLocal
                from models.database_models import CurrencyRate
                db = SessionLocal()
                try:
                    latest = db.query(CurrencyRate).order_by(CurrencyRate.rate_date.desc()).first()
                finally:
                    db.close()
                if latest is None or latest.rate_date < date.today():
                    logger.info("[NBP catch-up] rates stale/missing - fetching latest now")
                    _nbp_daily_job()

            threading.Thread(target=_catch_up, daemon=True).start()
        except Exception:
            logger.error("NBP catch-up not started", exc_info=True)

        # Same self-healing pattern for the LME web-sync: if the latest source='WEB'
        # bulletin is missing or older than the sync interval allows for, run it once
        # now instead of waiting for the next cron fire (covers a missed deploy window).
        if settings.LME_WEB_SYNC_ENABLED:
            try:
                import threading

                def _lme_web_catch_up():
                    from datetime import date, timedelta
                    from config.database import SessionLocal
                    from models.database_models import LMEBulletin
                    db = SessionLocal()
                    try:
                        latest = (
                            db.query(LMEBulletin)
                            .filter(LMEBulletin.source == 'WEB')
                            .order_by(LMEBulletin.bulletin_date.desc())
                            .first()
                        )
                    finally:
                        db.close()
                    stale_after = timedelta(days=6)  # cron default is ~5 days; 6 gives it slack
                    if latest is None or latest.bulletin_date < date.today() - stale_after:
                        logger.info("[LME web-sync catch-up] data stale/missing - syncing now")
                        _lme_web_sync_job()

                threading.Thread(target=_lme_web_catch_up, daemon=True).start()
            except Exception:
                logger.error("LME web-sync catch-up not started", exc_info=True)

        # Same self-healing pattern for the bulletin crawler: if the latest
        # source='MANUAL' bulletin (whether from a human upload or a prior crawler
        # run) is stale, run it once now rather than waiting for the next cron fire.
        if settings.LME_BULLETIN_CRAWLER_ENABLED:
            try:
                import threading

                def _lme_bulletin_crawler_catch_up():
                    from datetime import date, timedelta
                    from config.database import SessionLocal
                    from models.database_models import LMEBulletin
                    db = SessionLocal()
                    try:
                        latest = (
                            db.query(LMEBulletin)
                            .filter(LMEBulletin.source.in_(['MANUAL', 'CRAWLER']))
                            .order_by(LMEBulletin.bulletin_date.desc())
                            .first()
                        )
                    finally:
                        db.close()
                    stale_after = timedelta(days=8)  # site publishes roughly weekly; 8 gives it slack
                    if latest is None or latest.bulletin_date < date.today() - stale_after:
                        logger.info("[LME bulletin crawler catch-up] data stale/missing - crawling now")
                        _lme_bulletin_crawler_job()

                threading.Thread(target=_lme_bulletin_crawler_catch_up, daemon=True).start()
            except Exception:
                logger.error("LME bulletin crawler catch-up not started", exc_info=True)

    logger.info(f"{settings.APP_NAME} startup complete. API docs: /api/docs")

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown"""
    logger.info("Shutting down LME Monitoring System")

# Root endpoint
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/api/docs",
        "message": "Welcome to LME Monitoring System API"
    }

# Health check
@app.get("/health")
@limiter.exempt
async def health_check(db: Session = Depends(get_platform_db)):
    """Health check endpoint.

    Exempt from rate limiting: Railway's own health-check probe hits this on a
    fixed interval, and if it ever got a 429 here, Railway would conclude the
    app is unhealthy and restart it - the opposite of what this endpoint is for.
    """
    try:
        from sqlalchemy import text
        # Test database
        db.execute(text("SELECT 1"))
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    return {
        "status": "ok" if db_status == "healthy" else "degraded",
        "database": db_status,
        "version": settings.APP_VERSION
    }

# System info
@app.get("/api/system/info")
async def system_info():
    """Get system information"""
    return {
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "monitoring_days": settings.LC_MONITORING_DAYS,
        "features": {
            "multi_user": True,
            "whatsapp_alerts": True,
            "pdf_processing": True,
            "excel_import": True,
            "auto_expire": settings.AUTO_EXPIRE_ENABLED
        },
        # Harmless readiness flag (no secrets, no env details).
        "config": {
            "anthropic_key_configured": bool(settings.ANTHROPIC_API_KEY),
        },
        "api_endpoints": {
            "authentication": "/api/auth",
            "lc_management": "/api/lc",
            "alerts": "/api/alerts"
        }
    }

# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error": True}
    )

@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    """Handle the AppError hierarchy (core/exceptions.py) - the typed alternative
    to raising HTTPException ad hoc in every route."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error": True}
    )

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """FastAPI's default 422 response shape has `detail` as a list of Pydantic
    error objects. Existing frontend code (e.g. bank_limits.html's
    `toast(e.detail || 'Save failed')`) expects `detail` to be a single string,
    the same as every other handler here - reformat using the first error so
    Pydantic-validated endpoints don't silently show a broken error message.
    """
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = [str(p) for p in first.get("loc", []) if p != "body"]
        field = loc[-1] if loc else ""
        message = first.get("msg", "Invalid request.")
        detail = f"{field}: {message}" if field else message
    else:
        detail = "Invalid request."
    logger.info(f"Validation error on {request.method} {request.url.path}: {errors}")
    return JSONResponse(status_code=422, content={"detail": detail, "error": True})

@app.exception_handler(DataError)
async def data_error_handler(request, exc):
    """A value overflowed its column (StringDataRightTruncation) or had the wrong type.

    The full psycopg2 error names the exact column and value — log it, but never ship it
    to the browser; the user gets a clean, actionable message instead.
    """
    logger.error(f"Database value error on {request.method} {request.url.path}: "
                 f"{getattr(exc, 'orig', exc)}", exc_info=True)
    return JSONResponse(
        status_code=400,
        content={"detail": "One of the fields is longer than this system allows. "
                           "Please shorten it and try again.", "error": True}
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request, exc):
    """Any other database failure — log the detail, show the user a clean message."""
    logger.error(f"Database error on {request.method} {request.url.path}: "
                 f"{getattr(exc, 'orig', exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Could not save your changes. Please try again, or contact "
                           "support if this keeps happening.", "error": True}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Catch-all. The exception text can carry SQL, file paths or stack detail, so it goes
    to the server log only — the response body stays generic."""
    logger.error(f"Unhandled error on {request.method} {request.url.path}: "
                 f"{type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on the server. Please try again.",
                 "error": True}
    )

if __name__ == "__main__":
    import uvicorn

    logger.info("LME Monitoring System v2.0 - starting server")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )
