"""
Alert Generation Service
Triggered after PDF bulletin upload to detect LME price changes
for LCs opened within the last 40 days.
"""

from decimal import Decimal
from datetime import date, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

from models.database_models import PriceAlert
from infrastructure.formula_engine.lme_calculator import LMECalculator

logger = logging.getLogger("uvicorn")

ALERT_WINDOW_DAYS = 40


def _calculate_lme_for_bulletin(db: Session, bulletin_id: int, formula_number: int,
                                 product_code: str) -> Optional[float]:
    """
    Calculate LME for a product using a specific bulletin.
    Returns None on any failure — callers skip gracefully.
    """
    try:
        symbols = LMECalculator.get_symbols_for_product(formula_number, product_code)
        if not symbols:
            return None

        prices = LMECalculator.get_price_data(db, bulletin_id, symbols)
        if not prices:
            return None

        usd_rate, eur_rate = LMECalculator.get_currency_rates(db, bulletin_id)

        formula_map = {
            1:  lambda: LMECalculator.calculate_formula_1(prices, product_code),
            2:  lambda: LMECalculator.calculate_formula_2(prices, product_code),
            3:  lambda: LMECalculator.calculate_formula_3(prices, product_code),
            4:  lambda: LMECalculator.calculate_formula_4(prices, product_code),
            5:  lambda: LMECalculator.calculate_formula_5(prices, product_code),
            6:  lambda: LMECalculator.calculate_formula_6(prices, usd_rate, eur_rate, product_code),
            7:  lambda: LMECalculator.calculate_formula_7(prices, usd_rate, eur_rate, product_code),
            8:  lambda: LMECalculator.calculate_formula_8(prices, usd_rate, eur_rate, product_code),
            9:  lambda: LMECalculator.calculate_formula_9(prices, usd_rate, eur_rate, product_code),
            10: lambda: LMECalculator.calculate_formula_10(prices, usd_rate, eur_rate, product_code),
            11: lambda: LMECalculator.calculate_formula_11(prices, usd_rate, eur_rate, product_code),
        }

        if formula_number not in formula_map:
            return None

        return float(formula_map[formula_number]())

    except Exception as e:
        logger.debug(f"Calc failed — formula {formula_number}, product {product_code}: {e}")
        return None


def generate_alerts_for_bulletin(db: Session, bulletin_id: int) -> dict:
    """
    After a new PDF bulletin is uploaded, check all LCs opened within the
    last ALERT_WINDOW_DAYS days. For each LC product, recalculate LME
    using the new bulletin and compare to the stored value. If different,
    insert a PriceAlert record.

    Returns a summary dict: {alerts_created, skipped, errors}
    Does NOT commit — the caller is responsible for committing.
    """
    cutoff_date = date.today() - timedelta(days=ALERT_WINDOW_DAYS)

    rows = db.execute(text("""
        SELECT
            lm.lc_id,
            lm.lc_number,
            lp.line_id,
            lp.product_code,
            lp.origin,
            lp.quality,
            lp.lc_unit_price,
            lp.quantity,
            lp.current_lme,
            lp.baseline_lme
        FROM lc_master lm
        JOIN lc_products lp ON lm.lc_id = lp.lc_id
        WHERE lm.lc_date >= :cutoff
        AND lm.status NOT IN ('CLOSED', 'CANCELLED')
    """), {"cutoff": cutoff_date}).fetchall()

    alerts_created = 0
    skipped = 0
    errors = 0

    for row in rows:
        (lc_id, lc_number, line_id, product_code, origin, quality,
         lc_unit_price, quantity, current_lme, baseline_lme) = row

        # Compare current monitored LME against the baseline locked at import
        old_lme = (float(current_lme) if current_lme is not None
                   else (float(baseline_lme) if baseline_lme is not None else None))

        if old_lme is None:
            skipped += 1
            continue

        # Determine formula
        formula_number = LMECalculator.match_formula(origin, quality, product_code)
        if not formula_number:
            skipped += 1
            continue

        # Recalculate with the new bulletin
        new_lme = _calculate_lme_for_bulletin(db, bulletin_id, formula_number, product_code)
        if new_lme is None:
            skipped += 1
            continue

        old_r = round(old_lme, 2)
        new_r = round(new_lme, 2)

        if old_r == new_r:
            skipped += 1
            continue

        difference = round(new_r - old_r, 2)
        diff_pct = round((difference / old_r) * 100, 2) if old_r else None
        alert_type = 'INCREASE' if difference > 0 else 'DECREASE'

        try:
            alert = PriceAlert(
                lc_id=lc_id,
                line_id=line_id,
                alert_type=alert_type,
                priority='MEDIUM',
                old_lme_price=Decimal(str(old_r)),
                new_lme_price=Decimal(str(new_r)),
                lc_price=lc_unit_price if lc_unit_price is not None else Decimal('0'),
                difference=Decimal(str(difference)),
                difference_percent=Decimal(str(diff_pct)) if diff_pct is not None else None,
                quantity=quantity,
                whatsapp_sent=False,
            )
            db.add(alert)
            alerts_created += 1
        except Exception as e:
            logger.error(f"Failed to insert alert for LC {lc_number}: {e}")
            errors += 1

    logger.info(
        f"Alert generation — bulletin {bulletin_id}: "
        f"{alerts_created} created, {skipped} skipped, {errors} errors"
    )
    return {"alerts_created": alerts_created, "skipped": skipped, "errors": errors}
