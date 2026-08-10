"""
WhatsApp Alert Service — Meta WhatsApp Cloud API

Sends critical alerts to configured recipients (whatsapp_config table):
  - LME price-change alerts, delivered as a branded PDF document (see
    pdf_report.py) — every change, no threshold filtering.
  - Operational alerts (alert_engine SystemAlerts), delivered as grouped
    text — restricted to the curated CRITICAL_ALERT_TYPES allowlist, HIGH
    severity only (see _is_critical()). Routine/informational alerts stay
    dashboard-only.

Design decisions (from the parked WhatsApp plan):
  - Send to multiple individual recipients (Cloud API can't post to groups).
  - Grouped: all of a recipient's alerts go in ONE message, not one per alert.
  - Quiet hours are ignored — alerts always send.
  - Per-recipient thresholds/type filters come from whatsapp_config.

Two sending modes (config.WHATSAPP_USE_TEMPLATE):
  - free-form text/document -> only delivers inside the 24h customer window /
                       Meta test number. Use it to verify the pipeline today.
  - template        -> required for proactive business-initiated alerts in prod.
                       Needs an approved utility template (WHATSAPP_TEMPLATE_NAME
                       for text, WHATSAPP_PDF_TEMPLATE_NAME for the price-alert PDF).

Text/JSON calls use stdlib urllib (no extra dependency); the one binary
multipart call (media upload for the PDF) uses `requests` instead — see
_upload_media().
"""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal
from typing import Optional

import requests
from sqlalchemy.orm import Session

from config.settings import settings
from models.database_models import PriceAlert, WhatsAppConfig, SystemAlert
from infrastructure.whatsapp.pdf_report import build_price_alert_pdf

# Operational-alert severity ranking and icons.
_SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_SEVERITY_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟢"}

# WhatsApp-critical operational alert types — a deliberately curated subset of
# alert_engine's types, not just "severity == HIGH" (some HIGH types, like the
# 10-day MISSING_DOCUMENT heads-up, aren't urgent enough for a WhatsApp ping;
# see the plan notes for the full per-type rationale). Always requires HIGH
# severity in addition to being in this set — see _is_critical().
CRITICAL_ALERT_TYPES = {
    "ARRIVAL_DOCS_MISSING", "LC_EXPIRING", "LAST_SHIP_DATE", "SHORT_SHIPMENT",
    "DUTY_SURPRISE", "DEMURRAGE_ACCRUING", "DEMURRAGE_WARNING",
    "POST_SHIP_DOCS_MISSING", "FI_EXPIRY", "GD_LATE_FILED", "GD_FILING_OVERDUE",
    "GD_FILING_DUE", "EX_BOND_EXCEEDED", "SRO_QUOTA_EXCEEDED", "DA_DOC_ARRIVAL",
}

logger = logging.getLogger("uvicorn")

# Meta error returned when the recipient is outside the 24h window and you tried
# free-form text. Surfaced clearly so the cause is obvious during testing.
_OUTSIDE_WINDOW_HINT = (
    "recipient is outside the 24h window — they must message your number first, "
    "or switch to template mode (WHATSAPP_USE_TEMPLATE=true)"
)


# --------------------------------------------------------------------------- #
# Low-level Graph API
# --------------------------------------------------------------------------- #

def is_configured() -> bool:
    """True when the minimum credentials to send are present and enabled."""
    return bool(
        settings.WHATSAPP_ENABLED
        and settings.WHATSAPP_TOKEN
        and settings.WHATSAPP_PHONE_NUMBER_ID
    )


def _endpoint() -> str:
    return (
        f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}"
        f"/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )


def normalize_number(raw: str, country_code: Optional[str] = None) -> str:
    """
    Meta wants a bare international number: digits only, no '+', no spaces/dashes.
    A number that already carries a country code is left as-is; a local number
    gets the default (or recipient's) country code prepended.
    """
    if not raw:
        return ""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return ""

    cc = (country_code or settings.WHATSAPP_DEFAULT_COUNTRY_CODE or "").lstrip("+")
    cc_digits = "".join(ch for ch in cc if ch.isdigit())

    # If the recipient stored a leading 0 (local format), drop it before adding CC.
    if cc_digits and digits.startswith("0"):
        digits = digits.lstrip("0")
        digits = cc_digits + digits
    elif cc_digits and not digits.startswith(cc_digits):
        digits = cc_digits + digits
    return digits


def _token_fingerprint() -> str:
    """Last 6 chars only — enough to confirm .env was actually reloaded, never the full secret."""
    t = settings.WHATSAPP_TOKEN or ""
    return f"...{t[-6:]}" if len(t) >= 6 else "(empty)"


def _post(payload: dict) -> dict:
    """
    POST a message payload to the Graph API.
    Returns {"ok": bool, "message_id": str|None, "error": str|None, "raw": dict}.
    Never raises — callers record the result on the alert row.
    """
    url = _endpoint()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    logger.info("WhatsApp -> POST %s (token %s) payload=%s", url, _token_fingerprint(), payload)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        msg_id = (body.get("messages") or [{}])[0].get("id")
        logger.info("WhatsApp <- 200 OK message_id=%s", msg_id)
        return {"ok": True, "message_id": msg_id, "error": None, "raw": body}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            err = err_body.get("error", {})
            logger.error("WhatsApp <- %s %s: %s", e.code, e.reason, err_body)
            detail = err.get("message", str(e))
            code = err.get("code")
            if code in (131047, 131051) or "re-engagement" in detail.lower():
                detail = f"{detail} ({_OUTSIDE_WINDOW_HINT})"
            return {"ok": False, "message_id": None, "error": detail, "raw": err_body}
        except Exception:
            logger.error("WhatsApp <- %s %s (unparseable body)", e.code, e.reason)
            return {"ok": False, "message_id": None, "error": str(e), "raw": {}}
    except Exception as e:  # network / timeout / DNS
        logger.error("WhatsApp <- request failed before a response: %s", e)
        return {"ok": False, "message_id": None, "error": str(e), "raw": {}}


def send_text(to: str, body: str) -> dict:
    """Free-form text message (only delivers inside the 24h window / test number)."""
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    })


def send_template(to: str, body_param: str,
                  template_name: Optional[str] = None,
                  lang: Optional[str] = None) -> dict:
    """
    Send an approved template with a single body variable {{1}} = body_param.
    The template you get approved in WhatsApp Manager must therefore have exactly
    one body placeholder. See module docstring / setup notes for the recommended
    template text.
    """
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name or settings.WHATSAPP_TEMPLATE_NAME,
            "language": {"code": lang or settings.WHATSAPP_TEMPLATE_LANG},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": body_param}],
            }],
        },
    })


def _upload_media(file_bytes: bytes, filename: str, mime_type: str) -> Optional[str]:
    """
    Upload a file to Meta's media endpoint so it can be referenced by id in a
    document message (avoids needing a public URL to host the file).
    Uses `requests` — hand-rolling multipart/form-data over stdlib urllib for
    binary payloads is error-prone, so this one call is the exception to the
    rest of this module's stdlib-only approach.
    """
    url = f"{_endpoint().rsplit('/messages', 1)[0]}/media"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, file_bytes, mime_type)},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception:
        logger.exception("WhatsApp media upload failed")
        return None


def send_document(to: str, media_id: str, filename: str, caption: Optional[str] = None) -> dict:
    """Send an already-uploaded document by media id (free-form; needs the 24h window)."""
    doc: dict = {"id": media_id, "filename": filename}
    if caption:
        doc["caption"] = caption
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    })


def send_document_template(to: str, media_id: str, body_params: list,
                           template_name: Optional[str] = None,
                           lang: Optional[str] = None) -> dict:
    """
    Send an approved template whose header is a document, referencing the
    uploaded media id, with the given body variables in order.
    """
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name or settings.WHATSAPP_PDF_TEMPLATE_NAME,
            "language": {"code": lang or settings.WHATSAPP_TEMPLATE_LANG},
            "components": [
                {
                    "type": "header",
                    "parameters": [{"type": "document", "document": {"id": media_id}}],
                },
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                },
            ],
        },
    })


# --------------------------------------------------------------------------- #
# WhatsApp message template provisioning (Meta WABA templates API)
# --------------------------------------------------------------------------- #

def _upload_template_example_pdf() -> Optional[str]:
    """
    Build a realistic sample price-alert PDF and upload it via Meta's resumable
    upload API, returning the header handle a DOCUMENT-header template needs in
    its `example` so reviewers can see what the template looks like. This is a
    different upload path from _upload_media() (which is per-message, keyed to
    the phone number) — this one is app-level and only used at template-creation
    time.
    """
    from datetime import datetime as _dt
    from types import SimpleNamespace
    from infrastructure.whatsapp.pdf_report import build_price_alert_pdf

    sample_alert = SimpleNamespace(
        lc=SimpleNamespace(lc_number="LC-SAMPLE-001"), lc_id=1,
        product=SimpleNamespace(product_code="AL-INGOT"),
        alert_date=_dt.now(), alert_type="INCREASE",
        old_lme_price=2450.00, new_lme_price=2510.50,
        difference_percent=2.47, total_impact=1512.50,
    )
    pdf_bytes = build_price_alert_pdf([sample_alert], "Sample Recipient")

    try:
        init = requests.post(
            f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_APP_ID}/uploads",
            params={
                "file_name": "lme_price_alert_sample.pdf",
                "file_length": len(pdf_bytes),
                "file_type": "application/pdf",
            },
            headers={"Authorization": f"OAuth {settings.WHATSAPP_TOKEN}"},
            timeout=20,
        )
        init.raise_for_status()
        upload_session_id = init.json()["id"]

        upload = requests.post(
            f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{upload_session_id}",
            headers={
                "Authorization": f"OAuth {settings.WHATSAPP_TOKEN}",
                "file_offset": "0",
            },
            data=pdf_bytes,
            timeout=30,
        )
        upload.raise_for_status()
        return upload.json().get("h")
    except Exception:
        logger.exception("WhatsApp template sample-PDF upload failed")
        return None


def get_pdf_template_status() -> dict:
    """Look up the PDF-header template's current approval state at Meta."""
    if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_BUSINESS_ACCOUNT_ID):
        return {"exists": False, "status": None}
    url = (f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}"
           f"/{settings.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates")
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            params={"name": settings.WHATSAPP_PDF_TEMPLATE_NAME},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return {"exists": False, "status": None}
        return {"exists": True, "status": data[0].get("status")}
    except Exception:
        logger.exception("WhatsApp template status check failed")
        return {"exists": False, "status": None, "error": "lookup failed"}


def ensure_price_alert_pdf_template() -> dict:
    """
    Create the PDF-header WhatsApp template if it doesn't already exist.
    Meta reviews new templates asynchronously (usually minutes to a few
    hours) — call get_pdf_template_status() afterwards to poll for approval.
    """
    current = get_pdf_template_status()
    if current["exists"]:
        return {"created": False, **current}

    if not settings.WHATSAPP_APP_ID:
        return {"created": False, "exists": False, "status": None,
                "error": "WHATSAPP_APP_ID is not set — required to upload the template's sample PDF"}

    header_handle = _upload_template_example_pdf()
    if not header_handle:
        return {"created": False, "exists": False, "status": None,
                "error": "Failed to upload the sample PDF Meta requires for a document-header template"}

    url = (f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}"
           f"/{settings.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates")
    payload = {
        "name": settings.WHATSAPP_PDF_TEMPLATE_NAME,
        "language": settings.WHATSAPP_TEMPLATE_LANG,
        "category": "UTILITY",
        "components": [
            {
                "type": "HEADER",
                "format": "DOCUMENT",
                "example": {"header_handle": [header_handle]},
            },
            {
                "type": "BODY",
                "text": "Attached: {{1}} LME price alert(s) as of {{2}}.",
                "example": {"body_text": [["3", "27-Jul-2026 17:00"]]},
            },
        ],
    }
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if not resp.ok:
            detail = resp.json().get("error", {}).get("message", resp.text)
            return {"created": False, "exists": False, "status": None, "error": detail}
        return {"created": True, "exists": True, "status": "PENDING"}
    except Exception as e:
        logger.exception("WhatsApp template creation failed")
        return {"created": False, "exists": False, "status": None, "error": str(e)}


# --------------------------------------------------------------------------- #
# Recipient filtering
# --------------------------------------------------------------------------- #

def get_active_recipients(db: Session) -> list:
    return (
        db.query(WhatsAppConfig)
        .filter(WhatsAppConfig.active == True)  # noqa: E712
        .all()
    )


def recipient_wants_alert(recipient: WhatsAppConfig, alert: PriceAlert) -> bool:
    """Apply the recipient's type filter and amount/percent thresholds."""
    pref = (recipient.alert_type or "ALL").upper()
    if pref != "ALL" and pref != (alert.alert_type or "").upper():
        return False

    if recipient.min_amount_threshold is not None:
        impact = abs(alert.total_impact) if alert.total_impact is not None else 0
        if Decimal(str(impact)) < recipient.min_amount_threshold:
            return False

    if recipient.min_percent_threshold is not None:
        pct = abs(alert.difference_percent) if alert.difference_percent is not None else Decimal("0")
        if Decimal(str(pct)) < recipient.min_percent_threshold:
            return False

    return True


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _send_to_recipient(recipient: WhatsAppConfig, alerts: list) -> dict:
    """
    Send LME price alerts to one recipient as a branded PDF document (not
    text) — built fresh per batch, uploaded to Meta, then sent either as a
    plain document (free-form mode) or via the PDF-header template (proactive
    mode). Returns the _post()-shaped result.
    """
    to = normalize_number(recipient.phone_number, recipient.country_code)
    if not to:
        return {"ok": False, "message_id": None,
                "error": "recipient has no usable phone number", "raw": {}}

    pdf_bytes = build_price_alert_pdf(alerts, recipient.recipient_name)
    filename = f"LME_Price_Alert_{datetime.now():%Y%m%d_%H%M}.pdf"
    media_id = _upload_media(pdf_bytes, filename, "application/pdf")
    if not media_id:
        return {"ok": False, "message_id": None,
                "error": "PDF upload to WhatsApp failed", "raw": {}}

    count = len(alerts)
    when = f"{datetime.now():%d-%b-%Y %H:%M}"
    if settings.WHATSAPP_USE_TEMPLATE:
        return send_document_template(to, media_id, [str(count), when])
    caption = f"LME Price Alert — {count} item{'s' if count != 1 else ''} ({when}). See attached PDF for details."
    return send_document(to, media_id, filename, caption=caption)


def _mark_alerts(db: Session, alert_ids: set, status: str, message_id: Optional[str]):
    """Record delivery outcome on the alert rows. Caller commits."""
    if not alert_ids:
        return
    now = datetime.utcnow()
    for alert in db.query(PriceAlert).filter(PriceAlert.alert_id.in_(alert_ids)).all():
        alert.whatsapp_sent = status == "sent"
        alert.whatsapp_sent_at = now
        alert.whatsapp_delivery_status = status
        if message_id:
            alert.whatsapp_message_id = message_id


def send_pending_alerts(db: Session) -> dict:
    """
    Send every not-yet-sent alert to the recipients who want it, grouped per
    recipient. Called after bulletin upload generates alerts; also safe to call
    again as a retry (only picks up whatsapp_sent == False rows).

    Commits its own changes. Never raises — returns a summary dict.
    """
    summary = {"recipients": 0, "messages_sent": 0, "messages_failed": 0,
               "alerts_marked": 0, "skipped_reason": None, "errors": []}

    if not is_configured():
        summary["skipped_reason"] = "whatsapp not configured/enabled"
        return summary

    pending = (
        db.query(PriceAlert)
        .filter(PriceAlert.whatsapp_sent == False)  # noqa: E712
        .order_by(PriceAlert.alert_date.desc())
        .all()
    )
    if not pending:
        summary["skipped_reason"] = "no pending alerts"
        return summary

    recipients = get_active_recipients(db)
    if not recipients:
        summary["skipped_reason"] = "no active recipients"
        return summary
    summary["recipients"] = len(recipients)

    delivered_alert_ids: set = set()
    last_message_id: Optional[str] = None

    for recipient in recipients:
        wanted = [a for a in pending if recipient_wants_alert(recipient, a)]
        if not wanted:
            continue
        result = _send_to_recipient(recipient, wanted)
        if result["ok"]:
            summary["messages_sent"] += 1
            last_message_id = result["message_id"] or last_message_id
            delivered_alert_ids.update(a.alert_id for a in wanted)
            recipient.total_messages_sent = (recipient.total_messages_sent or 0) + 1
            recipient.last_message_sent = datetime.utcnow()
        else:
            summary["messages_failed"] += 1
            summary["errors"].append(f"{recipient.recipient_name}: {result['error']}")

    _mark_alerts(db, delivered_alert_ids, "sent", last_message_id)
    summary["alerts_marked"] = len(delivered_alert_ids)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        summary["errors"].append(f"commit failed: {e}")

    logger.info(
        "WhatsApp send — %s recipients, %s sent, %s failed, %s alerts marked",
        summary["recipients"], summary["messages_sent"],
        summary["messages_failed"], summary["alerts_marked"],
    )
    return summary


def send_single_alert(db: Session, alert_id: int) -> dict:
    """
    Manually (re)send one alert to all active recipients who want it.
    Ignores the whatsapp_sent flag (this is an explicit resend).
    Commits its own changes.
    """
    summary = {"alert_id": alert_id, "messages_sent": 0, "messages_failed": 0,
               "skipped_reason": None, "errors": []}

    if not is_configured():
        summary["skipped_reason"] = "whatsapp not configured/enabled"
        return summary

    alert = db.query(PriceAlert).filter(PriceAlert.alert_id == alert_id).first()
    if not alert:
        summary["skipped_reason"] = "alert not found"
        return summary

    recipients = get_active_recipients(db)
    if not recipients:
        summary["skipped_reason"] = "no active recipients"
        return summary

    last_message_id = None
    any_sent = False
    for recipient in recipients:
        if not recipient_wants_alert(recipient, alert):
            continue
        result = _send_to_recipient(recipient, [alert])
        if result["ok"]:
            summary["messages_sent"] += 1
            any_sent = True
            last_message_id = result["message_id"] or last_message_id
            recipient.total_messages_sent = (recipient.total_messages_sent or 0) + 1
            recipient.last_message_sent = datetime.utcnow()
        else:
            summary["messages_failed"] += 1
            summary["errors"].append(f"{recipient.recipient_name}: {result['error']}")

    if any_sent:
        _mark_alerts(db, {alert_id}, "sent", last_message_id)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        summary["errors"].append(f"commit failed: {e}")
    return summary


# --------------------------------------------------------------------------- #
# Operational alerts (alert_engine SystemAlerts)
# --------------------------------------------------------------------------- #

def _is_critical(alert: SystemAlert) -> bool:
    """
    WhatsApp-critical: HIGH severity AND in the curated CRITICAL_ALERT_TYPES
    allowlist. Severity alone isn't enough — some HIGH types (e.g. the 10-day
    MISSING_DOCUMENT heads-up) aren't urgent enough for a WhatsApp ping.
    """
    return ((alert.severity or "LOW").upper() == "HIGH"
            and alert.alert_type in CRITICAL_ALERT_TYPES)


def _system_alert_line(alert: SystemAlert) -> str:
    icon = _SEVERITY_ICON.get((alert.severity or "LOW").upper(), "•")
    return f"{icon} {alert.title}"


def format_system_message(alerts: list, recipient_name: Optional[str] = None,
                          for_template: bool = False) -> str:
    """
    Build the grouped operational digest for one recipient.
    Alerts are pre-sorted HIGH -> LOW by the caller.
    """
    greeting = f"Hello {recipient_name}, " if recipient_name else ""
    n = len(alerts)
    header = (f"{greeting}LME Ops — {n} item{'s' if n != 1 else ''} need attention "
              f"({datetime.now():%d-%b-%Y}):")
    lines = [_system_alert_line(a) for a in alerts]
    if for_template:
        return header + " " + " | ".join(lines)
    return header + "\n" + "\n".join(lines)


def _send_system_to_recipient(recipient: WhatsAppConfig, alerts: list) -> dict:
    to = normalize_number(recipient.phone_number, recipient.country_code)
    if not to:
        return {"ok": False, "message_id": None,
                "error": "recipient has no usable phone number", "raw": {}}
    if settings.WHATSAPP_USE_TEMPLATE:
        body = format_system_message(alerts, recipient.recipient_name, for_template=True)
        return send_template(to, body)
    body = format_system_message(alerts, recipient.recipient_name, for_template=False)
    return send_text(to, body)


def send_system_alerts(db: Session) -> dict:
    """
    Push not-yet-sent operational alerts (system_alerts, status=ACTIVE) to every
    active recipient, grouped into ONE message per recipient. Idempotent: only
    picks up whatsapp_sent == False rows, marks them sent after delivery.

    Commits its own changes. Never raises — returns a summary dict.
    """
    summary = {"recipients": 0, "messages_sent": 0, "messages_failed": 0,
               "alerts_marked": 0, "skipped_reason": None, "errors": []}

    if not is_configured():
        summary["skipped_reason"] = "whatsapp not configured/enabled"
        return summary

    pending = (
        db.query(SystemAlert)
        .filter(SystemAlert.status == "ACTIVE",
                SystemAlert.whatsapp_sent == False)  # noqa: E712
        .all()
    )
    pending = [a for a in pending if _is_critical(a)]
    if not pending:
        summary["skipped_reason"] = "no pending operational alerts"
        return summary

    # HIGH first, then by recency.
    pending.sort(key=lambda a: (_SEVERITY_RANK.get((a.severity or "LOW").upper(), 9),
                                -(a.created_at.timestamp() if a.created_at else 0)))

    recipients = get_active_recipients(db)
    if not recipients:
        summary["skipped_reason"] = "no active recipients"
        return summary
    summary["recipients"] = len(recipients)

    delivered_ids: set = set()
    for recipient in recipients:
        result = _send_system_to_recipient(recipient, pending)
        if result["ok"]:
            summary["messages_sent"] += 1
            delivered_ids.update(a.alert_id for a in pending)
            recipient.total_messages_sent = (recipient.total_messages_sent or 0) + 1
            recipient.last_message_sent = datetime.utcnow()
        else:
            summary["messages_failed"] += 1
            summary["errors"].append(f"{recipient.recipient_name}: {result['error']}")

    if delivered_ids:
        now = datetime.utcnow()
        for alert in db.query(SystemAlert).filter(SystemAlert.alert_id.in_(delivered_ids)).all():
            alert.whatsapp_sent = True
            alert.whatsapp_sent_at = now
        summary["alerts_marked"] = len(delivered_ids)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        summary["errors"].append(f"commit failed: {e}")

    logger.info(
        "WhatsApp ops-alerts — %s recipients, %s sent, %s failed, %s alerts marked",
        summary["recipients"], summary["messages_sent"],
        summary["messages_failed"], summary["alerts_marked"],
    )
    return summary


def send_test_message(to: str, country_code: Optional[str] = None) -> dict:
    """
    Fire a one-off test message to a single number. Useful for verifying
    credentials end-to-end before any alerts exist.
    Uses template mode if enabled, otherwise free-form text.
    """
    number = normalize_number(to, country_code)
    if not is_configured():
        return {"ok": False, "error": "whatsapp not configured/enabled", "message_id": None}
    if settings.WHATSAPP_USE_TEMPLATE:
        return send_template(number, "LME WhatsApp alerts are now connected ✅")
    return send_text(number, "✅ LME Monitoring — WhatsApp test message. Connection works!")


# --------------------------------------------------------------------------- #
# LME rates report — sent after a new bulletin is synced/crawled in
# --------------------------------------------------------------------------- #

# How far back to load bulletins when building the rates matrix. Only the newest
# row is printed, but the one before it is what the "change vs previous bulletin"
# column is computed from — so this just needs to comfortably span two bulletins.
_RATES_LOOKBACK_DAYS = 60


def send_rates_report(db: Session) -> dict:
    """
    Build the branded LME rates PDF (latest rate per region per product group,
    dated by the BULLETIN's own date) and send it to every active recipient.

    Idempotent and retryable, mirroring how send_pending_alerts/send_system_alerts
    self-heal: driven by LMEBulletin.whatsapp_rates_sent rather than always
    re-deriving "whatever's newest" — only the newest MANUAL/CRAWLER bulletin
    that hasn't been successfully reported yet is sent. Marked sent only once
    every active recipient succeeded, so a partial failure keeps retrying on the
    next call (e.g. the 15-minute scheduler) instead of being lost.

    Unlike price alerts, this report is identical for all recipients, so the PDF
    is built and uploaded once and the same media id is reused per send.
    Never raises — returns a summary dict.
    """
    summary = {"recipients": 0, "messages_sent": 0, "messages_failed": 0,
               "bulletin_date": None, "skipped_reason": None, "errors": []}

    if not is_configured():
        summary["skipped_reason"] = "whatsapp not configured/enabled"
        return summary

    recipients = get_active_recipients(db)
    if not recipients:
        summary["skipped_reason"] = "no active recipients"
        return summary
    summary["recipients"] = len(recipients)

    from models.database_models import LMEBulletin
    bulletin = (
        db.query(LMEBulletin)
        .filter(LMEBulletin.source.in_(["MANUAL", "CRAWLER"]))
        .order_by(LMEBulletin.bulletin_date.desc())
        .first()
    )
    if not bulletin:
        summary["skipped_reason"] = "no bulletins available to report"
        return summary
    if bulletin.whatsapp_rates_sent:
        summary["skipped_reason"] = "rates report already sent for latest bulletin"
        return summary

    bulletin_date = bulletin.bulletin_date.isoformat()
    summary["bulletin_date"] = bulletin_date

    try:
        from datetime import timedelta
        from modules.currency_rates.lme_rates_service import build_rates_matrix, PRODUCT_GROUPS
        from infrastructure.whatsapp.pdf_report import build_lme_rates_pdf, resolve_branding

        date_from = (datetime.now().date() - timedelta(days=_RATES_LOOKBACK_DAYS)).isoformat()
        rates_by_group = [
            build_rates_matrix(db, group, date_from, None) for group in PRODUCT_GROUPS
        ]
        pdf_bytes = build_lme_rates_pdf(rates_by_group, bulletin_date, resolve_branding(db))
    except Exception as e:
        logger.exception("WhatsApp rates report: failed to build PDF")
        summary["errors"].append(f"build failed: {e}")
        return summary

    filename = f"LME_Rates_{bulletin_date}.pdf"
    media_id = _upload_media(pdf_bytes, filename, "application/pdf")
    if not media_id:
        summary["errors"].append("PDF upload to WhatsApp failed")
        return summary

    when = f"{datetime.now():%d-%b-%Y %H:%M}"
    caption = f"LME Rates Report — bulletin dated {bulletin_date}. See attached PDF."

    for recipient in recipients:
        to = normalize_number(recipient.phone_number, recipient.country_code)
        if not to:
            summary["messages_failed"] += 1
            summary["errors"].append(f"{recipient.recipient_name}: no usable phone number")
            continue

        if settings.WHATSAPP_USE_TEMPLATE:
            result = send_document_template(to, media_id, [bulletin_date, when])
        else:
            result = send_document(to, media_id, filename, caption=caption)

        if result["ok"]:
            summary["messages_sent"] += 1
            recipient.total_messages_sent = (recipient.total_messages_sent or 0) + 1
            recipient.last_message_sent = datetime.utcnow()
        else:
            summary["messages_failed"] += 1
            summary["errors"].append(f"{recipient.recipient_name}: {result['error']}")

    if summary["messages_failed"] == 0 and summary["messages_sent"] > 0:
        bulletin.whatsapp_rates_sent = True
        bulletin.whatsapp_rates_sent_at = datetime.utcnow()

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        summary["errors"].append(f"commit failed: {e}")

    logger.info(
        "WhatsApp rates report (%s) — %s recipients, %s sent, %s failed",
        bulletin_date, summary["recipients"], summary["messages_sent"], summary["messages_failed"],
    )
    return summary


# --------------------------------------------------------------------------- #
# Delivery-status webhook (Meta -> us)
# --------------------------------------------------------------------------- #

def process_delivery_status_webhook(payload: dict, db: Session) -> int:
    """
    Handle a Meta WhatsApp webhook payload, updating whatsapp_delivery_status on
    every PriceAlert row matching a reported message id. A 200 OK at send-time
    only means Meta accepted the API call, not that the recipient received it
    (e.g. they're outside the 24h window) — this is what reports the real
    outcome (sent/delivered/read/failed) after the fact.
    Returns the number of PriceAlert rows updated. Never raises.
    """
    updated = 0
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for status_update in value.get("statuses", []):
                    message_id = status_update.get("id")
                    status = status_update.get("status")  # sent|delivered|read|failed
                    if not message_id or not status:
                        continue
                    errors = status_update.get("errors")
                    detail = status
                    if status == "failed" and errors:
                        detail = f"failed: {errors[0].get('title', errors[0].get('message', 'unknown error'))}"
                    rows = (
                        db.query(PriceAlert)
                        .filter(PriceAlert.whatsapp_message_id == message_id)
                        .all()
                    )
                    for alert in rows:
                        alert.whatsapp_delivery_status = detail
                        updated += 1
                    logger.info(
                        "WhatsApp webhook: message %s -> %s (%d alert row(s) updated)",
                        message_id, detail, len(rows),
                    )
        if updated:
            db.commit()
    except Exception:
        logger.exception("WhatsApp webhook processing failed")
        db.rollback()
    return updated
