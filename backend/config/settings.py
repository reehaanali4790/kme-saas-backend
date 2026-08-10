"""
LME Monitoring System - Configuration
Version: 2.0
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings
from typing import Literal, Optional
import json
import os


def _parse_allowed_origins(value):
    """Accept JSON arrays or comma-separated origin lists from Railway env vars."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            return json.loads(raw)
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return value

class Settings(BaseSettings):
    """Application Settings"""

    # Application
    APP_NAME: str = "LME Monitoring System"
    APP_VERSION: str = "2.0"
    APP_PUBLIC_URL: str = "http://localhost:3000"

    # Environment — must be set explicitly to "production" via env var (Railway).
    # Never defaults to production, so a misconfigured deploy fails DEBUG/CORS/
    # SECRET_KEY checks loudly instead of silently running unsafe in prod.
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True

    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/lme_monitoring"
    REDIS_URL: Optional[str] = None
    
    # Security — MUST be overridden via .env in production (e.g. SECRET_KEY=<random 64-char string>)
    SECRET_KEY: str = "your-secret-key-change-in-production-make-it-very-long-and-random"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480   # 8h; silent-refreshed by the frontend
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    
    # Password
    PWD_MIN_LENGTH: int = 8
    PWD_MAX_LENGTH: int = 100
    
    # File Upload
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024  # 50 MB
    ALLOWED_PDF_EXTENSIONS: list = [".pdf"]
    ALLOWED_EXCEL_EXTENSIONS: list = [".xlsx", ".xls", ".csv"]
    
    # Anthropic (Claude Vision — fallback for complex / failed extractions)
    ANTHROPIC_API_KEY: Optional[str] = None

    # Google Gemini (cheap/fast vision for scanned CI, PL, BL, etc.)
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-flash-latest"

    # Tiered extraction: Gemini -> Claude (see infrastructure.document_ai.document_ai.extract_document).
    # Text-parser tier is not wired up yet — flag is here so it activates without another
    # settings-shape change once services.text_parsers lands.
    EXTRACTION_TEXT_PARSER_ENABLED: bool = False
    EXTRACTION_GEMINI_ENABLED: bool = True
    EXTRACTION_CLAUDE_FALLBACK: bool = False
    EXTRACTION_TEXT_MIN_CONFIDENCE: float = 0.45

    # WhatsApp (Meta Cloud API)
    # Get these from developers.facebook.com -> your app -> WhatsApp -> API Setup
    WHATSAPP_ENABLED: bool = False                       # master on/off switch
    WHATSAPP_TOKEN: Optional[str] = None                 # access token (temp 24h, then permanent system-user token)
    WHATSAPP_PHONE_NUMBER_ID: Optional[str] = None       # the "From" number's ID (NOT the phone number itself)
    WHATSAPP_BUSINESS_ACCOUNT_ID: Optional[str] = None   # WABA ID (for managing templates)
    WHATSAPP_APP_ID: Optional[str] = None                 # Meta App ID (for uploading template sample media)
    WHATSAPP_API_VERSION: str = "v21.0"                  # Graph API version
    WHATSAPP_DEFAULT_COUNTRY_CODE: str = "+92"           # prepended to recipient numbers lacking one

    # Sending mode:
    #   False -> free-form text. Only works inside the 24h customer window / with the
    #            Meta test number. Use this to verify the pipeline end-to-end today.
    #   True  -> approved template. Required for proactive/business-initiated alerts in production.
    WHATSAPP_USE_TEMPLATE: bool = False
    WHATSAPP_TEMPLATE_NAME: str = "lme_price_alert"      # name of your approved utility template
    WHATSAPP_TEMPLATE_LANG: str = "en"                   # template language code (e.g. en, en_US)
    WHATSAPP_PDF_TEMPLATE_NAME: str = "lme_price_alert_pdf"  # document-header template for the branded PDF

    # Delivery-status webhook (Meta -> us): reports sent/delivered/read/failed per
    # message, since a 200 OK at send-time only means Meta accepted the API call,
    # not that the recipient actually received it (e.g. outside the 24h window).
    # Verify token is a value YOU make up and enter in both places (Meta's Configure
    # Webhooks screen and here) - Meta echoes it back on setup to prove you own the URL.
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: Optional[str] = None

    # Deprecated — Twilio path (kept so old .env files don't error). Unused.
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"
    
    # Email (Optional)
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    EMAIL_FROM: str = "noreply@lme-monitoring.com"
    
    # Monitoring
    LC_MONITORING_DAYS: int = 40
    AUTO_EXPIRE_ENABLED: bool = True

    # Background jobs — the APScheduler cron jobs (NBP rates, KPT crawlers) run
    # in-process and are NOT safe to run on more than one web instance at once
    # (every instance would fire its own copy of each job). Leave this on for a
    # single-instance deploy; set to False on every instance except one if the
    # web tier is ever scaled horizontally.
    ENABLE_SCHEDULER: bool = True

    # Rate limiting (slowapi).
    # GLOBAL_RATE_LIMIT is a baseline applied automatically to every endpoint that
    # doesn't declare its own @limiter.limit(...) (login/refresh below keep their
    # stricter, purpose-specific limits instead of this one - a route-specific
    # limit replaces the global default for that route, it doesn't stack with it).
    # Keyed per authenticated user where possible (see core/rate_limit.py), so
    # it's a per-person budget, not a per-office-IP one.
    GLOBAL_RATE_LIMIT: str = "300/minute"
    LOGIN_RATE_LIMIT: str = "10/minute"
    REFRESH_RATE_LIMIT: str = "30/minute"

    # KPT Expected Arrivals — auto-fill shipment ETA from kpt.gov.pk
    KPT_ETA_CRAWLER_ENABLED: bool = True
    KPT_ETA_URL: str = "https://kpt.gov.pk/pages/49/expected-arrivals"
    KPT_ETA_LC_AGE_DAYS: int = 23          # crawl when LC is at least this many days old
    KPT_ETA_SCHEDULE_HOUR: int = 9         # daily run (Pakistan time / UTC+5)
    KPT_ETA_SCHEDULE_MINUTE: int = 0

    # KPT Ships On Port — mark shipments "On port" when vessel is docked at KPT
    KPT_ON_PORT_CRAWLER_ENABLED: bool = True
    KPT_ON_PORT_URL: str = "https://kpt.gov.pk/pages/54/ship-on-port"

    # KPT Ship Departures — mark shipments "Departed" (runs every 6h after on-port check)
    KPT_DEPARTURES_CRAWLER_ENABLED: bool = True
    KPT_DEPARTURES_URL: str = "https://kpt.gov.pk/pages/53/ship-departures"
    KPT_PORT_CYCLE_HOURS: int = 6

    # KPT document alerts (BL/Invoice/Packing at ETA, GD on port, pickup after departure)
    KPT_DOC_ALERTS_ENABLED: bool = True
    KPT_DOC_ALERT_ACK_HOURS: int = 24       # re-alert after acknowledge if still missing
    KPT_DEPARTURE_PICKUP_DAYS: int = 3      # days after departure before pickup alert

    # Into-bond 180-day clock alerts (day 160 reminder, deadline passed, penalty due)
    BOND_ALERTS_ENABLED: bool = True
    BOND_ALERT_ACK_HOURS: int = 24          # re-alert after acknowledge if still unsettled

    # Operational alert engine (infrastructure/alerts/alert_engine.py) — used to be
    # manual-only (POST /api/alert-engine/scan); now also runs on a schedule so the
    # dashboard "Needs Attention" panel and WhatsApp alerts stay fresh without anyone
    # having to click "Trigger Alert Scan". The manual endpoint still works as before.
    ALERT_SCAN_ENABLED: bool = True
    ALERT_SCAN_INTERVAL_MINUTES: int = 15

    # thehelpers.pk LME bulletin auto-sync — supplements (never replaces) the manual
    # PDF upload flow. LME_WEB_SYNC_CRON is a standard 5-field cron expression; note
    # cron has no native "every N days" field, so "*/5" in the day-of-month slot fires
    # on calendar days divisible by 5 (resets each month) rather than a true rolling
    # 5-day interval — an accepted approximation of "every 5 days".
    LME_WEB_SYNC_ENABLED: bool = True
    LME_WEB_SYNC_URL: str = "https://thehelpers.pk/"
    LME_WEB_SYNC_CRON: str = "0 3 */5 * *"

    # thehelpers.pk bulletin auto-CRAWLER — a second, separate job from the WEB sync
    # above. thehelpers.pk's PDF actually embeds the full FastMarkets symbol page
    # (verified against a real bulletin), so this crawler downloads it and feeds it
    # through the EXACT SAME manual-upload pipeline (pdf_price_extractor +
    # pdf_upload_service) a human uses on /lc-upload's Upload Bulletin flow — it
    # never reimplements extraction, and the resulting bulletin is indistinguishable
    # from one someone uploaded by hand (source='MANUAL'), so the Rate Matrix and
    # cards update automatically. LME_WEB_SYNC above still separately feeds the
    # "Auto-Synced Reference Rates" panel from the same site's simpler duty-sheet
    # data - the two jobs are independent and can both run.
    LME_BULLETIN_CRAWLER_ENABLED: bool = True
    LME_BULLETIN_CRAWLER_CRON: str = "0 4 * * *"  # daily 04:00 PKT
    LME_BULLETIN_CRAWLER_LOOKBACK_DAYS: int = 2

    # VesselAPI — server-side vessel search and live AIS position lookup
    VESSEL_API_KEY: Optional[str] = None
    VESSEL_API_BASE_URL: str = "https://api.vesselapi.com/v1"
    VESSEL_API_TIMEOUT_SECONDS: int = 20

    # AISStream — free live AIS WebSocket fallback when VesselAPI misses / fails.
    # Scoped to Karachi / Northern Arabian Sea (Pakistan importer focus).
    AISSTREAM_API_KEY: Optional[str] = None
    AISSTREAM_URL: str = "wss://stream.aisstream.io/v0/stream"
    AISSTREAM_TIMEOUT_SECONDS: int = 45
    # Boxes as [[south, west], [north, east]] — near-port + inbound/outbound approaches
    AISSTREAM_BOUNDING_BOXES: list = [
        [[24.50, 66.70], [25.20, 67.60]],  # Karachi Port + Port Qasim
        [[20.00, 60.00], [26.50, 71.00]],  # Northern Arabian Sea approaches
    ]
    
    # Alerts
    ALERT_BATCH_SIZE: int = 100
    WHATSAPP_RATE_LIMIT: int = 10  # messages per minute
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "./logs/lme_system.log"

    # Stripe billing (SaaS)
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    STRIPE_PRICE_OPS_MONTHLY: Optional[str] = None
    STRIPE_PRICE_OPS_ANNUAL: Optional[str] = None
    STRIPE_PRICE_TD_MONTHLY: Optional[str] = None
    STRIPE_PRICE_TD_ANNUAL: Optional[str] = None
    PLATFORM_ADMIN_EMAILS: str = ""
    SIGNUP_RATE_LIMIT: str = "5/minute"
    DEFAULT_TRIAL_DAYS: int = 14
    
    # CORS — stored as a string so Railway/comma-separated env vars work on all
    # pydantic-settings versions. Use settings.cors_origins for the parsed list.
    ALLOWED_ORIGINS: str = "*"

    class Config:
        env_file = ".env"
        case_sensitive = True

    @property
    def cors_origins(self) -> list[str]:
        return _parse_allowed_origins(self.ALLOWED_ORIGINS)

    @model_validator(mode="after")
    def _validate_production_safety(self) -> "Settings":
        """Fail fast at startup rather than silently booting unsafe in production."""
        if self.ENVIRONMENT != "production":
            return self

        problems = []
        placeholder_key = "your-secret-key-change-in-production-make-it-very-long-and-random"
        if self.SECRET_KEY == placeholder_key or len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be set to a real random secret (32+ chars)")
        if self.DEBUG:
            problems.append("DEBUG must be false")
        if not self.cors_origins or "*" in self.cors_origins:
            problems.append("ALLOWED_ORIGINS must be an explicit list of origins, not '*' or empty")

        if problems:
            raise ValueError(
                "Refusing to start with ENVIRONMENT=production and unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )
        return self

# Initialize settings
settings = Settings()

# Railway/env robustness: pydantic's case-sensitive env resolution can miss a key that
# IS present in the raw process environment. Backfill the Anthropic key from os.environ
# so a correctly-set Railway variable is always honored regardless of source resolution.
if not settings.ANTHROPIC_API_KEY:
    settings.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("anthropic_api_key")
if not settings.GEMINI_API_KEY:
    settings.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

# Persistent storage detection on Railway / Docker volumes
railway_vol = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ("/data/uploads" if os.path.exists("/data") else None)
if railway_vol:
    settings.UPLOAD_DIR = railway_vol

# Create necessary directories
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "pdfs"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "excel"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "bl_documents"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "invoice_documents"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "packing_documents"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "gd_documents"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "fi_documents"), exist_ok=True)
os.makedirs(os.path.join(settings.UPLOAD_DIR, "branding"), exist_ok=True)
os.makedirs("./logs", exist_ok=True)
