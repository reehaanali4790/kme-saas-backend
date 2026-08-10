"""
Auto-sync thehelpers.pk's "LMB" duty-calculation bulletin into the system as a
source='WEB' LMEBulletin, alongside (never replacing) manually uploaded bulletins.

Shape mirrors integrations/nbp/nbp_rate_service.py: fetch an index page, find the
newest linked PDF, download it, extract, validate, persist. Difference from that
module: no AI vision tier needed here - helpers_bulletin_extractor parses the PDF
deterministically since the source publishes real text (not scanned images).
"""

import logging
import os
import tempfile
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.database import SessionLocal, engine
from config.settings import settings
from models.database_models import LMEBulletin, LMEPriceHistory, LMESyncRun
from modules.documents.extractors.helpers_bulletin_extractor import extract_from_pdf

logger = logging.getLogger("uvicorn")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# Session-level Postgres advisory lock - held for the life of one raw connection,
# so a scheduled run and a concurrent "Sync Now" click can't race even across
# multiple web instances (unlike an in-process threading.Lock).
_LOCK_KEY = "lme_web_sync"


def _find_latest_pdf_link(index_html: str, base_url: str) -> tuple[str, "datetime.date | None"]:
    """Parse the 'LME Per Ton Duty Calculation' sidebar list; return
    (absolute_pdf_url, date_from_the_anchor_text_or_None) for the first (newest) link.
    Never reconstructs a filename from a date - real filenames are inconsistently
    spaced ("...The Helpers.pdf" vs "...TheHelpers.pdf")."""
    soup = BeautifulSoup(index_html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/uploads/pdf/" in href and href.lower().endswith(".pdf"):
            anchor_date = None
            try:
                anchor_date = datetime.strptime(a.get_text(strip=True), "%d-%b-%Y").date()
            except ValueError:
                pass
            url = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
            return url, anchor_date
    raise ValueError("No LME bulletin PDF link found on thehelpers.pk (site layout may have changed).")


def _try_acquire_lock(conn) -> bool:
    return bool(conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": _LOCK_KEY}).scalar())


def _release_lock(conn) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": _LOCK_KEY})
    conn.commit()


def sync_web_bulletin(trigger: str, triggered_by: int | None = None) -> dict:
    """
    Fetch, extract, validate and store the latest thehelpers.pk bulletin.

    trigger: 'CRON' or 'MANUAL'. Never raises - always returns a result dict and
    always writes an LMESyncRun row, so failures are visible without crashing the
    scheduler or the manual "Sync Now" endpoint. On any failure, existing
    LMEBulletin/LMEPriceHistory rows are left untouched (old data stays live).
    """
    db: Session = SessionLocal()
    lock_conn = engine.connect()
    got_lock = False

    run = LMESyncRun(trigger=trigger, triggered_by=triggered_by, status='RUNNING',
                      source_url=settings.LME_WEB_SYNC_URL)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        got_lock = _try_acquire_lock(lock_conn)
        lock_conn.commit()
        if not got_lock:
            run.status = 'SKIPPED_NO_NEW_DATA'
            run.error_message = 'Another sync run is already in progress.'
            run.finished_at = datetime.utcnow()
            db.commit()
            logger.info("thehelpers_lme_service: sync skipped - another run holds the lock")
            return {"status": run.status, "message": run.error_message}

        resp = httpx.get(settings.LME_WEB_SYNC_URL, headers=UA, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        pdf_url, anchor_date = _find_latest_pdf_link(resp.text, settings.LME_WEB_SYNC_URL)
        run.source_url = pdf_url
        db.commit()

        pdf_resp = httpx.get(pdf_url, headers=UA, timeout=30, follow_redirects=True)
        pdf_resp.raise_for_status()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            tmp.write(pdf_resp.content)
            tmp.close()
            extraction = extract_from_pdf(tmp.name, expected_date_hint=anchor_date)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

        bulletin_date = extraction["bulletin_date"]
        run.bulletin_date_found = bulletin_date

        existing = db.query(LMEBulletin).filter(
            LMEBulletin.bulletin_date == bulletin_date,
            LMEBulletin.source == 'WEB',
        ).first()
        if existing:
            run.status = 'SKIPPED_NO_NEW_DATA'
            run.finished_at = datetime.utcnow()
            db.commit()
            logger.info(f"thehelpers_lme_service: {bulletin_date} already synced - nothing to do")
            return {"status": run.status, "bulletin_date": str(bulletin_date)}

        bulletin = LMEBulletin(
            bulletin_date=bulletin_date,
            file_name=pdf_url.rsplit("/", 1)[-1],
            file_path=pdf_url,
            file_size=len(pdf_resp.content),
            source='WEB',
            symbols_extracted=len(extraction["rows"]),
            prices_stored=0,
            extracted_data={"rows": extraction["rows"]},
        )
        db.add(bulletin)
        db.flush()

        for row in extraction["rows"]:
            db.add(LMEPriceHistory(
                bulletin_id=bulletin.bulletin_id,
                bulletin_date=bulletin_date,
                symbol=row["hs_code"],
                product_type='STEEL',
                product_code=row["group"],
                quality=row["quality"],
                origin=row["alloy"],
                low_price=row["price"],
                high_price=row["price"],
                avg_price=row["price"],
                notes='thehelpers.pk auto-sync',
            ))
        bulletin.prices_stored = len(extraction["rows"])

        run.status = 'SUCCESS'
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.info(f"thehelpers_lme_service: synced {bulletin_date} - {bulletin.prices_stored} rows stored")
        return {
            "status": "SUCCESS",
            "bulletin_date": str(bulletin_date),
            "prices_stored": bulletin.prices_stored,
            "groups_found": sorted(extraction["groups_found"]),
        }

    except Exception as exc:
        db.rollback()
        run.status = 'FAILED'
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.utcnow()
        try:
            db.commit()
        except Exception:
            db.rollback()
        logger.error(f"thehelpers_lme_service: sync failed - {exc}", exc_info=True)
        return {"status": "FAILED", "error": str(exc)}

    finally:
        if got_lock:
            try:
                _release_lock(lock_conn)
            except Exception:
                pass
        lock_conn.close()
        db.close()
