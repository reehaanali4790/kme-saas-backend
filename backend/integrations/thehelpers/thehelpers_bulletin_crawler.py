"""
Automated bulletin crawler: thehelpers.pk -> the existing manual-upload pipeline.

thehelpers.pk's own "LMB" PDF embeds a full FastMarkets symbol page (verified against
a real bulletin), so this crawler doesn't reimplement extraction at all - it downloads
the PDF and feeds it through the exact same two steps a human triggers from the Upload
Bulletin screen: pdf_price_extractor.extract_prices_from_pdf() then
pdf_upload_service.replace_bulletin_prices() (+ alerts/WhatsApp/LC recalc). The result
is a bulletin with source='MANUAL', identical to one someone uploaded by hand - the
Rate Matrix and cards pick it up with no other code changes.

Separate job from integrations/thehelpers/thehelpers_lme_service.py: that one syncs
the site's simpler duty-sheet data (source='WEB') into the "Auto-Synced Reference
Rates" panel. Both target the same site, run independently, and don't affect the
same LMEBulletin (source, ('bulletin_date', 'source') unique constraint keeps them
apart).

Only ever looks at the last LME_BULLETIN_CRAWLER_LOOKBACK_DAYS days of bulletins on
each run - the site's sidebar lists many months of history, most already imported.
"""

import logging
import os
import tempfile
import time
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.database import SessionLocal, engine
from config.settings import settings
from models.database_models import CurrencyRate, LMEBulletin, LMEBulletinCrawlLog
from modules.documents import pdf_upload_service as svc
from modules.documents.extractors.pdf_price_extractor import extract_prices_from_pdf

logger = logging.getLogger("uvicorn")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Separate advisory-lock key from thehelpers_lme_service's "lme_web_sync" - these are
# two independent jobs and neither should block on the other.
_LOCK_KEY = "lme_bulletin_crawler"

DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = 5


def _find_recent_pdf_links(index_html: str, base_url: str, since: date) -> list[tuple[str, "date | None"]]:
    """Parse the 'LME Per Ton Duty Calculation' sidebar list; return every
    (absolute_pdf_url, anchor_date) whose anchor date is on/after `since`, newest
    first. A link whose anchor text doesn't parse as a date is kept (so a format
    change on the site doesn't silently hide a real bulletin) - dedup/date-filtering
    for those falls back to the PDF's own internal date once downloaded."""
    soup = BeautifulSoup(index_html, "html.parser")
    found: list[tuple[str, "date | None"]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/uploads/pdf/" not in href or not href.lower().endswith(".pdf"):
            continue
        anchor_date = None
        try:
            anchor_date = datetime.strptime(a.get_text(strip=True), "%d-%b-%Y").date()
        except ValueError:
            pass
        if anchor_date is not None and anchor_date < since:
            continue
        url = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
        found.append((url, anchor_date))
    return found


def _already_imported(db: Session, bulletin_date: date) -> bool:
    """The crawler feeds the same Rate Matrix a human upload does, so dedup checks
    for either kind of row - LMEBulletin itself is the source of truth, no separate
    tracking table needed for this check. MANUAL and CRAWLER are tagged separately
    (source='CRAWLER' for this pipeline) purely so the UI can show which bulletins
    were auto-imported vs. uploaded by a person; both feed the same matrix."""
    return db.query(LMEBulletin).filter(
        LMEBulletin.bulletin_date == bulletin_date,
        LMEBulletin.source.in_(['MANUAL', 'CRAWLER']),
    ).first() is not None


def _pick_currency_rate(db: Session, bulletin_date: date) -> "CurrencyRate | None":
    """Exact-date match, else the closest earlier rate, else the latest available -
    same fallback order as pdf_upload_router's /check-rates uses for a human."""
    exact = db.query(CurrencyRate).filter(CurrencyRate.rate_date == bulletin_date).first()
    if exact:
        return exact
    earlier = (db.query(CurrencyRate)
                 .filter(CurrencyRate.rate_date <= bulletin_date)
                 .order_by(CurrencyRate.rate_date.desc()).first())
    if earlier:
        return earlier
    return db.query(CurrencyRate).order_by(CurrencyRate.rate_date.desc()).first()


def _download_with_retry(url: str) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            resp = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            logger.warning(
                f"thehelpers_bulletin_crawler: download attempt {attempt}/{DOWNLOAD_RETRIES} "
                f"failed for {url}: {exc}"
            )
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
    raise last_exc  # type: ignore[misc]


def _try_acquire_lock(conn) -> bool:
    return bool(conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": _LOCK_KEY}).scalar())


def _release_lock(conn) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": _LOCK_KEY})
    conn.commit()


def _log_attempt(db: Session, trigger: str, status: str, *, bulletin_date_found=None,
                  source_url=None, file_name=None, bulletin_id=None, error_message=None) -> None:
    row = LMEBulletinCrawlLog(
        trigger=trigger, status=status, finished_at=datetime.utcnow(),
        bulletin_date_found=bulletin_date_found, source_url=source_url,
        file_name=file_name, bulletin_id=bulletin_id,
        error_message=(error_message[:2000] if error_message else None),
    )
    db.add(row)
    db.commit()


def _import_one_bulletin(db: Session, url: str) -> dict:
    """Download, extract, and store one candidate PDF via the existing manual-upload
    service functions. Returns a result dict; never raises - callers get status back
    instead so one bad bulletin doesn't stop the rest of the run."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp_path = tmp.name
    tmp.close()
    try:
        content = _download_with_retry(url)
        with open(tmp_path, "wb") as f:
            f.write(content)

        extraction = extract_prices_from_pdf(tmp_path)
        bulletin_date = extraction.get("bulletin_date")
        if not bulletin_date:
            _log_attempt(db, "CRON", "FAILED", source_url=url,
                         error_message="Could not extract bulletin date from PDF.")
            return {"status": "FAILED", "url": url, "error": "no bulletin_date"}
        bulletin_date = datetime.strptime(bulletin_date, "%Y-%m-%d").date() \
            if isinstance(bulletin_date, str) else bulletin_date

        if _already_imported(db, bulletin_date):
            _log_attempt(db, "CRON", "SKIPPED_ALREADY_IMPORTED",
                         bulletin_date_found=bulletin_date, source_url=url)
            return {"status": "SKIPPED_ALREADY_IMPORTED", "bulletin_date": str(bulletin_date)}

        if not extraction.get("prices"):
            _log_attempt(db, "CRON", "FAILED", bulletin_date_found=bulletin_date, source_url=url,
                         error_message="No FastMarkets symbols found in PDF.")
            return {"status": "FAILED", "url": url, "error": "no prices extracted"}

        currency_rate = _pick_currency_rate(db, bulletin_date)
        if not currency_rate:
            _log_attempt(db, "CRON", "FAILED", bulletin_date_found=bulletin_date, source_url=url,
                         error_message="No currency rate available (exact, earlier, or latest).")
            return {"status": "FAILED", "url": url, "error": "no currency rate"}

        file_name = url.rsplit("/", 1)[-1]
        bulletin, prices_stored = svc.replace_bulletin_prices(
            db, bulletin_date, file_name, None,  # uploaded_by=None: system-triggered, no human user
            len(extraction["symbols_found"]), extraction["prices"], currency_rate,
            source='CRAWLER')

        alert_summary = svc.generate_bulletin_alerts(db, bulletin.bulletin_id)
        svc.dispatch_whatsapp_alerts(db, alert_summary["alerts_created"])
        whatsapp_rates = svc.dispatch_whatsapp_rates_report(db)
        svc.recalculate_lcs_for_bulletin(bulletin_date, bulletin.bulletin_id)

        _log_attempt(db, "CRON", "SUCCESS", bulletin_date_found=bulletin_date, source_url=url,
                     file_name=file_name, bulletin_id=bulletin.bulletin_id)
        logger.info(
            f"thehelpers_bulletin_crawler: imported {bulletin_date} - "
            f"{prices_stored} price(s), {alert_summary['alerts_created']} alert(s)"
        )
        return {"status": "SUCCESS", "bulletin_date": str(bulletin_date), "prices_stored": prices_stored,
                "whatsapp_rates_report": whatsapp_rates}

    except Exception as exc:
        db.rollback()
        logger.error(f"thehelpers_bulletin_crawler: failed importing {url}: {exc}", exc_info=True)
        try:
            _log_attempt(db, "CRON", "FAILED", source_url=url, error_message=str(exc))
        except Exception:
            db.rollback()
        return {"status": "FAILED", "url": url, "error": str(exc)}
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def crawl_and_import_bulletins(trigger: str = "CRON") -> dict:
    """Entry point for the scheduler (and a manual-trigger endpoint, if one is added
    later). Never raises. Looks back LME_BULLETIN_CRAWLER_LOOKBACK_DAYS days, imports
    every not-yet-seen bulletin found in that window through the manual pipeline."""
    db: Session = SessionLocal()
    lock_conn = engine.connect()
    got_lock = False
    summary = {"found": 0, "imported": 0, "skipped": 0, "failed": 0, "results": []}

    try:
        got_lock = _try_acquire_lock(lock_conn)
        lock_conn.commit()
        if not got_lock:
            logger.info("thehelpers_bulletin_crawler: skipped - another run holds the lock")
            return {"status": "SKIPPED_LOCKED"}

        since = date.today() - timedelta(days=settings.LME_BULLETIN_CRAWLER_LOOKBACK_DAYS)
        logger.info(f"thehelpers_bulletin_crawler: starting (trigger={trigger}, since={since})")

        try:
            resp = httpx.get(settings.LME_WEB_SYNC_URL, headers=UA, timeout=20, follow_redirects=True)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.error(f"thehelpers_bulletin_crawler: index fetch failed: {exc}")
            _log_attempt(db, trigger, "FAILED", error_message=f"Index fetch failed: {exc}")
            return {"status": "FAILED", "error": str(exc)}

        candidates = _find_recent_pdf_links(resp.text, settings.LME_WEB_SYNC_URL, since)
        summary["found"] = len(candidates)
        logger.info(f"thehelpers_bulletin_crawler: {len(candidates)} candidate(s) in the last "
                    f"{settings.LME_BULLETIN_CRAWLER_LOOKBACK_DAYS} day(s)")

        for url, anchor_date in candidates:
            result = _import_one_bulletin(db, url)
            summary["results"].append(result)
            if result["status"] == "SUCCESS":
                summary["imported"] += 1
            elif result["status"] == "SKIPPED_ALREADY_IMPORTED":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1

        logger.info(
            f"thehelpers_bulletin_crawler: done - found={summary['found']} "
            f"imported={summary['imported']} skipped={summary['skipped']} failed={summary['failed']}"
        )
        return summary

    finally:
        if got_lock:
            try:
                _release_lock(lock_conn)
            except Exception:
                pass
        lock_conn.close()
        db.close()
