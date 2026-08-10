"""Business logic for LME calculation, extracted from
modules/currency_rates/lme_calculation_router.py as part of the Phase 4 module rollout.

calculate_lc/test_formula_match raise plain ValueError (not an AppError) for the two
cases that mapped to the original's HTTPException(400) - no AppError subclass models a
generic 400, so the router translates ValueError -> HTTPException(400) itself (same
approach as modules/documents/pdf_upload_service.py / modules/currency_rates/service.py).
bulletin_impact raises NotFoundError, matching the original's HTTPException(404).
"""
from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError
from infrastructure.formula_engine.formula_engine import FormulaEngine
from infrastructure.formula_engine.lme_calculator import LMECalculator
from modules.currency_rates.lme_calculation_schemas import LCForCalculation

BULLETIN_IMPACT_WINDOW_DAYS = 40


def list_lcs_for_calculation(db: Session) -> List[LCForCalculation]:
    query = text("""
        SELECT
            lm.lc_id,
            lm.lc_number,
            lp.lc_unit_price,
            lp.current_lme,
            lp.product_code,
            lp.origin,
            lp.quality,
            lp.baseline_lme,
            lm.lc_date,
            lm.status,
            lp.grade,
            lp.product_name
        FROM lc_master lm
        LEFT JOIN lc_products lp ON lm.lc_id = lp.lc_id
        ORDER BY lm.lc_date DESC
    """)
    results = db.execute(query).fetchall()

    lcs = []
    processed_lc_ids = set()

    for row in results:
        lc_id = row[0]
        if lc_id in processed_lc_ids:
            continue
        processed_lc_ids.add(lc_id)

        formula_number = None
        if row[4] and row[5] and row[6]:
            formula_number = FormulaEngine.determine_formula(row[4], row[5], row[6], row[10], row[11])

        current_lme = float(row[3]) if row[3] else None
        baseline_lme = float(row[7]) if row[7] else None
        lc_unit_price = float(row[2]) if row[2] else None
        display_lme = current_lme or baseline_lme
        difference = round(display_lme - lc_unit_price, 2) if display_lme and lc_unit_price else None

        lcs.append(LCForCalculation(
            lc_id=lc_id,
            lc_number=row[1],
            lc_date=str(row[8]) if row[8] else None,
            status=row[9],
            lc_unit_price=lc_unit_price,
            current_lme=current_lme,
            baseline_lme=baseline_lme,
            product_code=row[4],
            origin=row[5],
            quality=row[6],
            formula_number=formula_number,
            difference=difference
        ))

    return lcs


def calculate_lc(db: Session, lc_id: int) -> dict:
    """Raises ValueError (converted to 400 by the router) on calculation failure -
    matches the original's `except ValueError as e: raise HTTPException(400, ...)`."""
    result = LMECalculator.calculate_single_lc(db, lc_id)

    db.execute(text("""
        UPDATE lc_products
        SET current_lme = :lme_value, last_lme_update = CURRENT_TIMESTAMP
        WHERE lc_id = :lc_id
    """), {"lme_value": result["lme_value"], "lc_id": lc_id})
    db.commit()

    return {
        "success": True,
        "lc_id": lc_id,
        "lc_number": result["lc_number"],
        "lme_value": result["lme_value"],
        "formula_number": result["formula_number"],
        "bulletin_id": result["bulletin_id"],
        "symbols_used": result["symbols_used"],
        "product_code": result["product_code"],
        "origin": result["origin"],
        "quality": result["quality"]
    }


def test_formula_match(
    db: Session, product_code: str, origin: str, quality: str,
    grade: Optional[str] = None, product_name: Optional[str] = None,
) -> dict:
    formula_number = FormulaEngine.determine_formula(product_code, origin, quality, grade, product_name)
    if not formula_number:
        raise ValueError(
            f"Could not match formula for product_code={product_code!r}, origin={origin!r}, "
            f"quality={quality!r} — this combination isn't mapped to any formula."
        )

    symbols = LMECalculator.get_symbols_for_product(formula_number, product_code)

    result = db.execute(text("""
        SELECT formula_description FROM calculation_formulas WHERE formula_id = :formula_id
    """), {"formula_id": formula_number}).fetchone()
    formula_name = result[0] if result else f"Formula {formula_number}"

    return {
        "formula_number": formula_number,
        "formula_name": formula_name,
        "required_symbols": symbols,
    }


def calculate_all_lcs(db: Session) -> dict:
    results = db.execute(text(
        "SELECT lc_id FROM lc_master WHERE status != 'CLOSED'")).fetchall()
    lc_ids = [row[0] for row in results]

    # One batched fetch of every LC/bulletin/price/rate needed, instead of calling
    # calculate_single_lc() (up to 4 queries each) once per LC in this loop.
    calc_results = LMECalculator.calculate_batch(db, lc_ids)

    success_count = 0
    error_count = 0
    errors = []

    for lc_id in lc_ids:
        result = calc_results.get(lc_id, {"error": "no result"})
        if "error" in result:
            error_count += 1
            errors.append({"lc_id": lc_id, "error": result["error"]})
            continue
        db.execute(text("""
            UPDATE lc_products
            SET current_lme = :lme_value, last_lme_update = CURRENT_TIMESTAMP
            WHERE lc_id = :lc_id
        """), {"lme_value": result["lme_value"], "lc_id": lc_id})
        success_count += 1

    db.commit()

    return {
        "success": True,
        "total_processed": success_count + error_count,
        "successful": success_count,
        "failed": error_count,
        "errors": errors if errors else None
    }


def bulletin_impact(db: Session, bulletin_id: int) -> dict:
    bul = db.execute(
        text("SELECT bulletin_id, bulletin_date FROM lme_bulletins WHERE bulletin_id = :bid"),
        {"bid": bulletin_id}
    ).fetchone()
    if not bul:
        raise NotFoundError("Bulletin not found")

    cutoff = date.today() - timedelta(days=BULLETIN_IMPACT_WINDOW_DAYS)

    rows = db.execute(text("""
        SELECT
            lm.lc_id, lm.lc_number, lm.lc_date, lm.status,
            lp.product_code, lp.origin, lp.quality,
            lp.baseline_lme, lp.current_lme
        FROM lc_master lm
        LEFT JOIN lc_products lp ON lm.lc_id = lp.lc_id
        WHERE lm.lc_date >= :cutoff
          AND lm.status != 'CANCELLED'
        ORDER BY lm.lc_date DESC
    """), {"cutoff": cutoff}).fetchall()

    lc_ids = []
    row_by_lc = {}
    for row in rows:
        lc_id = row[0]
        if lc_id in row_by_lc:
            continue
        row_by_lc[lc_id] = row
        lc_ids.append(lc_id)

    # One batched fetch of prices/rates for this bulletin, shared across every LC in the
    # window, instead of calculate_single_lc() re-fetching them once per LC.
    calc_results = LMECalculator.calculate_batch(db, lc_ids, bulletin_id=bulletin_id)

    results = []
    for lc_id in lc_ids:
        row = row_by_lc[lc_id]
        baseline_lme = float(row[7]) if row[7] is not None else None
        current_lme  = float(row[8]) if row[8] is not None else None

        new_lme = None
        formula_number = None
        error_msg = None

        result = calc_results.get(lc_id, {"error": "no result"})
        if "error" in result:
            error_msg = result["error"]
        else:
            new_lme = round(result["lme_value"], 2)
            formula_number = result["formula_number"]

        diff_baseline = round(new_lme - baseline_lme, 2) if new_lme is not None and baseline_lme is not None else None
        diff_current  = round(new_lme - current_lme,  2) if new_lme is not None and current_lme  is not None else None

        results.append({
            "lc_id":          lc_id,
            "lc_number":      row[1],
            "lc_date":        str(row[2]) if row[2] else None,
            "status":         row[3],
            "product_code":   row[4],
            "origin":         row[5],
            "quality":        row[6],
            "formula_number": formula_number,
            "baseline_lme":   baseline_lme,
            "current_lme":    current_lme,
            "new_lme":        new_lme,
            "diff_baseline":  diff_baseline,
            "diff_current":   diff_current,
            "error":          error_msg,
        })

    return {
        "success": True,
        "bulletin_id": bulletin_id,
        "bulletin_date": str(bul[1]),
        "cutoff_date": str(cutoff),
        "lcs": results,
    }


def apply_rates(db: Session, bulletin_id: int, lc_ids: List[int]) -> dict:
    if lc_ids:
        target_ids = lc_ids
    else:
        cutoff = date.today() - timedelta(days=BULLETIN_IMPACT_WINDOW_DAYS)
        rows = db.execute(text("""
            SELECT lm.lc_id FROM lc_master lm
            WHERE lm.lc_date >= :cutoff AND lm.status != 'CANCELLED'
            ORDER BY lm.lc_date DESC
        """), {"cutoff": cutoff}).fetchall()
        target_ids = [r[0] for r in rows]

    # One batched fetch of prices/rates for this bulletin, shared across every LC being
    # applied, instead of calculate_single_lc() re-fetching them once per LC.
    calc_results = LMECalculator.calculate_batch(db, target_ids, bulletin_id=bulletin_id)

    success_count = 0
    skipped_count = 0
    errors = []

    for lc_id in target_ids:
        result = calc_results.get(lc_id, {"error": "no result"})
        if "error" in result:
            skipped_count += 1
            errors.append({"lc_id": lc_id, "error": result["error"]})
            continue
        db.execute(text("""
            UPDATE lc_products
            SET current_lme = :lme_value, last_lme_update = CURRENT_TIMESTAMP
            WHERE lc_id = :lc_id
        """), {"lme_value": result["lme_value"], "lc_id": lc_id})
        success_count += 1

    db.commit()

    return {
        "success": True,
        "bulletin_id": bulletin_id,
        "applied": success_count,
        "skipped": skipped_count,
        "errors": errors if errors else None,
    }
