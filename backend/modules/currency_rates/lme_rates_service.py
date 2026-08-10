"""Business logic for the LME rates matrix, extracted from
modules/currency_rates/lme_rates_router.py as part of the Phase 4 module rollout.
"""
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.database_models import LMEBulletin, LMEPriceHistory, CurrencyRate
from infrastructure.formula_engine.formula_engine import FormulaEngine
from infrastructure.formula_engine.lme_calculator import LMECalculator

logger = logging.getLogger("uvicorn")

# Valid formula sets per display region
# UAE includes 1: formula_engine.determine_formula() maps UAE-origin HRP to formula 1
# (UAE HRP uses the same china-prime formula - "unambiguously prime-grade by code alone",
# see its own comment) - this set must stay in sync with that or a correctly-computed
# price gets silently discarded here.
REGION_VALID_FORMULAS = {
    "CHINA":  {1, 2, 3, 4, 5},
    "AFRICA": {8, 9},
    "EUROPE": {6, 7},
    "UAE":    {1, 10, 11},
}

REGION_ORIGIN = {
    "CHINA":  "CHINA",
    "AFRICA": "SOUTH AFRICA",
    "EUROPE": "EUROPE",
    "UAE":    "UAE",
}

# Product groups: each group defines col1 and col2 (col2 can be None)
PRODUCT_GROUPS = {
    'HR': {
        'col1': {'label': 'Prime',     'product': 'HRP',   'quality': 'PRIME'},
        'col2': {'label': 'Secondary', 'product': 'HRS',   'quality': 'SECONDARY'},
    },
    'GP': {
        'col1': {'label': 'Prime',     'product': 'GPP',   'quality': 'PRIME'},
        'col2': {'label': 'Secondary', 'product': 'GPS',   'quality': 'SECONDARY'},
    },
    'CR': {
        'col1': {'label': 'Prime',     'product': 'CRP',   'quality': 'PRIME'},
        'col2': {'label': 'Secondary', 'product': 'CRS',   'quality': 'SECONDARY'},
    },
    'CRNGO': {
        'col1': {'label': 'Secondary', 'product': 'CRNGO', 'quality': 'SECONDARY'},
        'col2': None,
    },
    'WR': {
        'col1': {'label': 'LC',        'product': 'WRLC',  'quality': 'PRIME'},
        'col2': {'label': 'HC',        'product': 'WRHC',  'quality': 'PRIME'},
    },
}

REGIONS = ["CHINA", "AFRICA", "EUROPE", "UAE"]


def get_rate_for_bulletin(bulletin: LMEBulletin, db: Session):
    """Get currency rate for bulletin, falling back to the closest available rate."""
    if bulletin.rate_id:
        rate = db.query(CurrencyRate).filter(CurrencyRate.rate_id == bulletin.rate_id).first()
        if rate:
            return rate

    # Fallback: closest rate by date (on or before bulletin date)
    rate = db.query(CurrencyRate).filter(
        CurrencyRate.rate_date <= bulletin.bulletin_date
    ).order_by(desc(CurrencyRate.rate_date)).first()

    if not rate:
        # Last resort: any rate
        rate = db.query(CurrencyRate).order_by(desc(CurrencyRate.rate_date)).first()

    return rate


def calc_region(product_code: str, quality: str, region_key: str,
                prices: dict, usd_rate: Decimal, eur_rate: Decimal) -> Optional[float]:
    """Calculate LME for product+quality at a given region. Returns None if not applicable."""
    origin = REGION_ORIGIN[region_key]
    formula_num = FormulaEngine.determine_formula(product_code, origin, quality)

    if formula_num not in REGION_VALID_FORMULAS[region_key]:
        return None

    try:
        if formula_num == 1:
            result = LMECalculator.calculate_formula_1(prices, product_code)
        elif formula_num == 2:
            result = LMECalculator.calculate_formula_2(prices, product_code)
        elif formula_num == 3:
            result = LMECalculator.calculate_formula_3(prices, product_code)
        elif formula_num == 4:
            result = LMECalculator.calculate_formula_4(prices, product_code)
        elif formula_num == 5:
            result = LMECalculator.calculate_formula_5(prices, product_code)
        elif formula_num == 6:
            result = LMECalculator.calculate_formula_6(prices, usd_rate, eur_rate, product_code)
        elif formula_num == 7:
            result = LMECalculator.calculate_formula_7(prices, usd_rate, eur_rate, product_code)
        elif formula_num == 8:
            result = LMECalculator.calculate_formula_8(prices, usd_rate, eur_rate, product_code)
        elif formula_num == 9:
            result = LMECalculator.calculate_formula_9(prices, usd_rate, eur_rate, product_code)
        elif formula_num == 10:
            result = LMECalculator.calculate_formula_10(prices, usd_rate, eur_rate, product_code)
        elif formula_num == 11:
            result = LMECalculator.calculate_formula_11(prices, usd_rate, eur_rate, product_code)
        else:
            return None
        return round(float(result), 2)
    except Exception as e:
        logger.debug(f"Calc failed [{region_key}] f{formula_num} {product_code}: {e}")
        return None


def build_rates_matrix(db: Session, group: str, date_from: Optional[str],
                       date_to: Optional[str]) -> dict:
    group = group.upper()
    if group not in PRODUCT_GROUPS:
        group = "HR"

    group_def = PRODUCT_GROUPS[group]
    col1 = group_def["col1"]
    col2 = group_def["col2"]

    # FastMarkets-formula matrix considers MANUAL (human-uploaded) and CRAWLER
    # (auto-imported by integrations/thehelpers/thehelpers_bulletin_crawler.py)
    # bulletins - both carry full FastMarkets symbol data for FormulaEngine to compute
    # against. The separate, simpler auto-synced WEB bulletins (source='WEB') have no
    # regional symbol data and are shown in their own panel instead (see
    # lme_web_rates_service.py) - without this filter the newest WEB bulletin can
    # become the "latest" row and blank out every region card with None.
    query = db.query(LMEBulletin).filter(LMEBulletin.source.in_(['MANUAL', 'CRAWLER']))

    if date_from:
        try:
            query = query.filter(
                LMEBulletin.bulletin_date >= datetime.strptime(date_from, "%Y-%m-%d").date()
            )
        except ValueError:
            pass

    if date_to:
        try:
            query = query.filter(
                LMEBulletin.bulletin_date <= datetime.strptime(date_to, "%Y-%m-%d").date()
            )
        except ValueError:
            pass

    bulletins = query.order_by(LMEBulletin.bulletin_date.asc()).all()
    logger.info(f"LME Rates Matrix: {len(bulletins)} bulletins for group={group}")

    field_names = []
    for region in REGIONS:
        field_names.append(f"{region.lower()}_col1")
        if col2:
            field_names.append(f"{region.lower()}_col2")

    matrix = []
    for bulletin in bulletins:
        rate = get_rate_for_bulletin(bulletin, db)
        if not rate:
            logger.warning(f"No currency rate for bulletin {bulletin.bulletin_id} ({bulletin.bulletin_date}) — skipping")
            continue

        price_records = db.query(LMEPriceHistory).filter(
            LMEPriceHistory.bulletin_id == bulletin.bulletin_id
        ).all()

        prices = {
            r.symbol: (Decimal(str(r.low_price)), Decimal(str(r.high_price)))
            for r in price_records
            if r.low_price is not None and r.high_price is not None
        }

        usd_rate = Decimal(str(rate.usd_rate))
        eur_rate = Decimal(str(rate.eur_rate))

        row = {
            "bulletin_date": bulletin.bulletin_date.isoformat(),
            "bulletin_id":   bulletin.bulletin_id,
        }

        for region in REGIONS:
            row[f"{region.lower()}_col1"] = calc_region(
                col1["product"], col1["quality"], region, prices, usd_rate, eur_rate
            )
            row[f"{region.lower()}_col2"] = calc_region(
                col2["product"], col2["quality"], region, prices, usd_rate, eur_rate
            ) if col2 else None

        matrix.append(row)

    # Compute change vs previous bulletin for each field
    for i, row in enumerate(matrix):
        for field in field_names:
            prev = matrix[i - 1][field] if i > 0 else None
            if prev is None or row[field] is None:
                row[f"{field}_chg"] = None
            else:
                row[f"{field}_chg"] = round(row[field] - prev, 2)

    matrix.reverse()  # newest first for table

    return {
        "matrix":          matrix,
        "bulletin_count":  len(matrix),
        "group":           group,
        "col1_label":      col1["label"],
        "col2_label":      col2["label"] if col2 else None,
        "groups":          list(PRODUCT_GROUPS.keys()),
    }
