"""Business logic for FastMarkets PDF bulletin upload, extracted from
modules/documents/pdf_upload_router.py as part of the Phase 4 module rollout.
"""
import logging
from datetime import datetime, timedelta, date
from typing import Optional

from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from config.database import SessionLocal
from core.exceptions import NotFoundError
from models.database_models import LMEBulletin, LMEPriceHistory, CurrencyRate
from infrastructure.alerts.alert_service import generate_alerts_for_bulletin
from infrastructure.formula_engine.lme_calculator import LMECalculator

logger = logging.getLogger("uvicorn")

MAX_PDF_SIZE = 10 * 1024 * 1024  # 10MB

REGION_MAP = {
    'MB-STE-0144': 'CHINA',
    'MB-STE-0145': 'CHINA',
    'MB-STE-0009': 'CHINA',
    'MB-STE-0148': 'CHINA',
    'MB-STE-0026': 'EUROPE_NORTH',
    'MB-STE-0027': 'EUROPE_SOUTH',
    'MB-STE-0030': 'EUROPE_NORTH',
    'MB-STE-0031': 'EUROPE_SOUTH',
    'MB-STE-0028': 'EUROPE_NORTH',
    'MB-STE-0892': 'EUROPE_ITALY',
    'MB-STE-0893': 'EUROPE_SPAIN',
    'MB-STE-0053': 'EUROPE_NORTH',
    'MB-STE-0054': 'EUROPE_SOUTH',
    'MB-STE-0124': 'UAE',
    'MB-STE-0123': 'UAE',
    'MB-STE-0125': 'UAE',
    'MB-STE-0181': 'USA',
    'MB-STE-0182': 'USA',
    'MB-STE-0014': 'CIS',
    'MB-STE-0017': 'CIS',
    'MB-STE-0120': 'TURKEY',
}


def is_valid_pdf(filename: str) -> bool:
    return filename.lower().endswith('.pdf')


def get_region_from_symbol(symbol: str) -> str:
    return REGION_MAP.get(symbol, 'UNKNOWN')


def rate_availability(db: Session, bulletin_date) -> dict:
    exact_rate = db.query(CurrencyRate).filter(CurrencyRate.rate_date == bulletin_date).first()
    if exact_rate:
        return {
            "success": True,
            "rates_found": True,
            "bulletin_date": str(bulletin_date),
            "rate": {
                "rate_id": exact_rate.rate_id,
                "rate_date": str(exact_rate.rate_date),
                "usd_rate": float(exact_rate.usd_rate),
                "eur_rate": float(exact_rate.eur_rate),
                "source": exact_rate.source
            },
            "message": f"Currency rates found for {bulletin_date}"
        }

    latest_rate = db.query(CurrencyRate).order_by(desc(CurrencyRate.rate_date)).first()
    return {
        "success": True,
        "rates_found": False,
        "bulletin_date": str(bulletin_date),
        "latest_rate": {
            "rate_date": str(latest_rate.rate_date),
            "usd_rate": float(latest_rate.usd_rate),
            "eur_rate": float(latest_rate.eur_rate)
        } if latest_rate else None,
        "message": f"No rates found for {bulletin_date}. Please enter rates."
    }


def resolve_currency_rate(db: Session, bulletin_date, rate_id: Optional[int],
                          usd_rate: Optional[float], eur_rate: Optional[float],
                          user_id: int, username: str) -> CurrencyRate:
    """Raises ValueError (not an AppError) for the two bad-request cases here, since
    they map to the original's plain HTTPException(400) - no AppError subclass models
    a generic 400, so the router translates ValueError -> HTTPException(400) itself."""
    if rate_id:
        currency_rate = db.query(CurrencyRate).filter(CurrencyRate.rate_id == rate_id).first()
        if not currency_rate:
            raise ValueError("Invalid rate_id")
        return currency_rate

    if usd_rate and eur_rate:
        existing_rate = db.query(CurrencyRate).filter(CurrencyRate.rate_date == bulletin_date).first()
        if existing_rate:
            return existing_rate
        currency_rate = CurrencyRate(
            rate_date=bulletin_date,
            usd_rate=usd_rate,
            eur_rate=eur_rate,
            source='PDF Upload',
            notes=f'Created during PDF upload by {username}',
            created_by=user_id
        )
        db.add(currency_rate)
        db.flush()
        return currency_rate

    raise ValueError("Currency rates required: provide rate_id OR (usd_rate AND eur_rate)")


def replace_bulletin_prices(db: Session, bulletin_date, filename: str, user_id: Optional[int],
                            symbols_found: int, prices_data: list,
                            currency_rate: CurrencyRate, source: str = 'MANUAL') -> tuple[LMEBulletin, int]:
    # Scoped to MANUAL/CRAWLER (both carry full FastMarkets symbol data, unlike the
    # separate simpler WEB duty-sheet sync) so re-uploading a bulletin for a date that
    # also has a WEB bulletin replaces only the MANUAL/CRAWLER one - a human re-uploading
    # a date the crawler already imported replaces that row rather than leaving a
    # duplicate; the crawler's own dedup check means the reverse never happens.
    existing_bulletin = db.query(LMEBulletin).filter(
        LMEBulletin.bulletin_date == bulletin_date,
        LMEBulletin.source.in_(['MANUAL', 'CRAWLER'])).first()
    if existing_bulletin:
        db.query(LMEPriceHistory).filter(
            LMEPriceHistory.bulletin_id == existing_bulletin.bulletin_id).delete()
        db.delete(existing_bulletin)
        db.flush()

    bulletin = LMEBulletin(
        bulletin_date=bulletin_date,
        file_name=filename,
        upload_date=datetime.now(),
        uploaded_by=user_id,
        symbols_extracted=symbols_found,
        prices_stored=0,
        rate_id=currency_rate.rate_id,
        source=source,
    )
    db.add(bulletin)
    db.flush()

    prices_stored = 0
    for price_data in prices_data:
        db.add(LMEPriceHistory(
            bulletin_id=bulletin.bulletin_id,
            bulletin_date=bulletin_date,
            symbol=price_data['symbol'],
            product_type='STEEL',
            region=get_region_from_symbol(price_data['symbol']),
            low_price=price_data['low'],
            high_price=price_data['high'],
            avg_price=price_data['avg']
        ))
        prices_stored += 1

    bulletin.prices_stored = prices_stored
    db.commit()
    return bulletin, prices_stored


def generate_bulletin_alerts(db: Session, bulletin_id: int) -> dict:
    """Non-critical: price-change alerts for LCs opened in the last 40 days."""
    alert_summary = {"alerts_created": 0, "skipped": 0, "errors": 0}
    try:
        alert_summary = generate_alerts_for_bulletin(db, bulletin_id)
        if alert_summary["alerts_created"] > 0:
            db.commit()
    except Exception as alert_err:
        logger.warning(f"Alert generation failed (upload still succeeded): {alert_err}")
        try:
            db.rollback()
        except Exception:
            pass
    return alert_summary


def dispatch_whatsapp_alerts(db: Session, alerts_created: int):
    """Non-critical: push new alerts over WhatsApp. No-op unless WhatsApp is enabled."""
    if alerts_created <= 0:
        return
    try:
        from infrastructure.whatsapp.whatsapp_service import send_pending_alerts
        wa_summary = send_pending_alerts(db)
        logger.info(f"WhatsApp dispatch: {wa_summary}")
    except Exception as wa_err:
        logger.warning(f"WhatsApp dispatch failed (upload still succeeded): {wa_err}")
        try:
            db.rollback()
        except Exception:
            pass


def dispatch_whatsapp_rates_report(db: Session) -> dict:
    """
    Non-critical: push the branded LME rates PDF over WhatsApp after a new
    bulletin lands. Separate from dispatch_whatsapp_alerts — that one reports
    the impact on our LCs and only fires when there are alerts, this one reports
    the newly published rates themselves and fires on every new bulletin.
    No-op unless WhatsApp is enabled. Returns the send summary (or an error
    dict) so callers can surface whether a PDF actually went out, rather than
    this being a silent fire-and-forget side effect.
    """
    try:
        from infrastructure.whatsapp.whatsapp_service import send_rates_report
        summary = send_rates_report(db)
        logger.info(f"WhatsApp rates report: {summary}")
        return summary
    except Exception as err:
        logger.warning(f"WhatsApp rates report failed (upload still succeeded): {err}")
        try:
            db.rollback()
        except Exception:
            pass
        return {"error": str(err)}


def autofill_pending_baselines(db: Session, bulletin_date, bulletin_id: int) -> tuple[int, int]:
    """Auto-fill baseline_lme for LCs pending due to missing bulletin (LC date within
    14 days before this bulletin). Returns (baselines_filled, baseline_pending)."""
    baselines_filled = 0
    baseline_pending = 0
    try:
        bd = bulletin_date if isinstance(bulletin_date, date) else datetime.strptime(str(bulletin_date), "%Y-%m-%d").date()
        max_lc_date = bd + timedelta(days=14)
        null_lc_ids = db.execute(text("""
            SELECT DISTINCT lm.lc_id
            FROM lc_master lm
            JOIN lc_products lp ON lm.lc_id = lp.lc_id
            WHERE lp.baseline_lme IS NULL
              AND lm.lc_date >= :bulletin_date
              AND lm.lc_date <= :max_lc_date
        """), {"bulletin_date": bd, "max_lc_date": max_lc_date}).fetchall()

        for (lc_id,) in null_lc_ids:
            try:
                calc_result = LMECalculator.calculate_single_lc(db, lc_id)
                db.execute(text("""
                    UPDATE lc_products
                    SET baseline_lme = :lme_value,
                        baseline_bulletin_id = :bulletin_id,
                        last_lme_update = CURRENT_TIMESTAMP
                    WHERE lc_id = :lc_id AND baseline_lme IS NULL
                """), {
                    "lme_value": calc_result["lme_value"],
                    "bulletin_id": calc_result["bulletin_id"],
                    "lc_id": lc_id
                })
                baselines_filled += 1
            except Exception as e:
                logger.warning(f"Auto-fill baseline failed for lc_id {lc_id}: {e}")
                baseline_pending += 1

        if baselines_filled > 0:
            db.commit()
            logger.info(f"Auto-filled baseline LME for {baselines_filled} pending LCs from bulletin {bulletin_id}")
    except Exception as fill_err:
        logger.warning(f"Baseline auto-fill failed (upload still succeeded): {fill_err}")
        try:
            db.rollback()
        except Exception:
            pass
    return baselines_filled, baseline_pending


def recalc_active_lc_lme(db: Session, bulletin_id: int) -> int:
    """Auto-recalculate current_lme for all active LCs against this new bulletin."""
    current_lme_updated = 0
    try:
        active_lc_ids = db.execute(text(
            "SELECT lc_id FROM lc_master WHERE status != 'CLOSED'")).fetchall()
        for (lc_id,) in active_lc_ids:
            try:
                result = LMECalculator.calculate_single_lc(db, lc_id, bulletin_id=bulletin_id)
                db.execute(text("""
                    UPDATE lc_products
                    SET current_lme = :lme_value, last_lme_update = CURRENT_TIMESTAMP
                    WHERE lc_id = :lc_id
                """), {"lme_value": result["lme_value"], "lc_id": lc_id})
                current_lme_updated += 1
            except Exception as e:
                # LCs whose products need a symbol this bulletin doesn't carry are skipped
                logger.warning(f"Auto current_lme recalc skipped for lc_id {lc_id}: {e}")
        if current_lme_updated > 0:
            db.commit()
            logger.info(f"Auto-recalculated current_lme for {current_lme_updated} active LC(s) "
                        f"from bulletin {bulletin_id}")
    except Exception as recalc_err:
        logger.warning(f"current_lme auto-recalc failed (upload still succeeded): {recalc_err}")
        try:
            db.rollback()
        except Exception:
            pass
    return current_lme_updated


def recalculate_lcs_for_bulletin(bulletin_date, bulletin_id: int) -> None:
    """Runs as a FastAPI BackgroundTask after the upload-bulletin response is sent.

    autofill_pending_baselines() and recalc_active_lc_lme() each loop per-LC with
    individual synchronous queries - against a remote DB that's dozens to hundreds of
    milliseconds away, that loop can run to a minute or more once there are a
    meaningful number of LCs. Neither of their return values is shown in the upload
    UI, so there's nothing lost by letting them finish after the response instead of
    blocking it. Opens its own session since the request-scoped one from get_db is
    already closed by the time a background task runs.
    """
    db = SessionLocal()
    try:
        baselines_filled, baseline_pending = autofill_pending_baselines(db, bulletin_date, bulletin_id)
        current_lme_updated = recalc_active_lc_lme(db, bulletin_id)
        logger.info(
            f"Background LC recalc for bulletin {bulletin_id} complete - "
            f"baselines_filled={baselines_filled}, baseline_pending={baseline_pending}, "
            f"current_lme_updated={current_lme_updated}"
        )
    except Exception as e:
        logger.error(f"Background LC recalc failed for bulletin {bulletin_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def bulletins_list(db: Session, limit: int) -> dict:
    total = db.query(LMEBulletin).count()
    bulletins = db.query(LMEBulletin).order_by(desc(LMEBulletin.bulletin_date)).limit(limit).all()
    return {
        "success": True,
        "total": total,
        "data": [
            {
                "bulletin_id": b.bulletin_id,
                "bulletin_date": str(b.bulletin_date),
                "file_name": b.file_name,
                "upload_date": b.upload_date.isoformat(),
                "symbols_extracted": b.symbols_extracted,
                "prices_stored": b.prices_stored,
                "source": b.source
            }
            for b in bulletins
        ]
    }


def delete_bulletin(db: Session, bulletin_id: int):
    bulletin = db.query(LMEBulletin).filter(LMEBulletin.bulletin_id == bulletin_id).first()
    if not bulletin:
        raise NotFoundError("Bulletin not found")
    db.query(LMEPriceHistory).filter(LMEPriceHistory.bulletin_id == bulletin_id).delete()
    db.delete(bulletin)
    db.commit()
