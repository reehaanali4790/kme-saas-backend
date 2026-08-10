"""
LME Calculator Service
Handles all LME price calculations with database integration
"""

import bisect
from typing import Optional, Dict, List, Tuple
from decimal import Decimal
from datetime import datetime, date
from sqlalchemy.orm import Session
from sqlalchemy import text

# Import FormulaEngine for formula matching
from infrastructure.formula_engine.formula_engine import FormulaEngine


class LMECalculator:
    """
    LME Price Calculator with Database Integration
    Uses FormulaEngine for formula matching, implements all 11 calculation formulas
    """

    @staticmethod
    def get_symbols_for_product(formula_number: int, product_code: str) -> List[str]:
        """
        Get required Metal Bulletin symbols for a given formula and product
        This handles product-specific symbol routing within formulas
        """

        # Formula 1: China PRIME (HRP/CRP/PPGI/GPP/GLP/GP)
        if formula_number == 1:
            if product_code == 'HRP':
                return ['MB-STE-0144']  # HRC China
            elif product_code == 'CRP':
                return ['MB-STE-0145']  # CRC China
            elif product_code in ['PPGI', 'PPGIP', 'GPP', 'GLP', 'GP']:
                return ['MB-STE-0009']  # Galvanized China
            else:
                return []

        # Formula 2: China SECONDARY (HRS/CRS/GPS/GLS/PPGIS)
        elif formula_number == 2:
            if product_code == 'HRS':
                return ['MB-STE-0144']  # HRC China
            elif product_code == 'CRS':
                return ['MB-STE-0145']  # CRC China
            elif product_code in ['GPS', 'GLS', 'PPGIS']:
                return ['MB-STE-0009']  # Galvanized China
            else:
                return []

        # Formula 3: China SECONDARY (CRNGO only)
        elif formula_number == 3:
            return ['MB-STE-0145']  # CRC China

        # Formula 4: China PRIME (WRLC)
        elif formula_number == 4:
            return ['MB-STE-0148']  # Wire rod China

        # Formula 5: China PRIME (WRHC)
        elif formula_number == 5:
            return ['MB-STE-0148']  # Wire rod China

        # Formula 6: Europe SECONDARY (CRS/GPS)
        elif formula_number == 6:
            if product_code == 'CRS':
                return ['MB-STE-0026', 'MB-STE-0027']  # CRC North/South Europe
            elif product_code == 'GPS':
                return ['MB-STE-0030', 'MB-STE-0031']  # HDG North/South Europe
            else:
                return []

        # Formula 7: Europe SECONDARY (HRS)
        elif formula_number == 7:
            return ['MB-STE-0028', 'MB-STE-0892', 'MB-STE-0893']  # North/Italy/Spain

        # Formula 8: S.Africa/Taiwan SECONDARY (CRS/GPS)
        elif formula_number == 8:
            if product_code == 'CRS':
                return ['MB-STE-0026', 'MB-STE-0027', 'MB-STE-0124', 'MB-STE-0181', 'MB-STE-0145']
            elif product_code == 'GPS':
                return ['MB-STE-0030', 'MB-STE-0031', 'MB-STE-0123', 'MB-STE-0182', 'MB-STE-0009']
            else:
                return []

        # Formula 9: S.Africa/Taiwan SECONDARY (HRS)
        elif formula_number == 9:
            return ['MB-STE-0028', 'MB-STE-0892', 'MB-STE-0893', 'MB-STE-0014', 'MB-STE-0125', 'MB-STE-0144']

        # Formula 10: UAE/Iran PRIME (WRLC)
        elif formula_number == 10:
            return ['MB-STE-0053', 'MB-STE-0054', 'MB-STE-0017', 'MB-STE-0120', 'MB-STE-0148']

        # Formula 11: UAE/Iran PRIME (WRHC)
        elif formula_number == 11:
            return ['MB-STE-0053', 'MB-STE-0054', 'MB-STE-0017', 'MB-STE-0120', 'MB-STE-0148']

        # Formula 12: Plastic (PMC) — TODO: placeholder symbol pending the real PMC
        # bulletin series from the business; must exist in lme_price_history under this
        # symbol for Formula 12 to resolve any prices.
        elif formula_number == 12:
            return ['MB-PMC-0001']

        return []

    @staticmethod
    def match_formula(
        origin: str, quality: str, product_code: str,
        grade: Optional[str] = None, product_name: Optional[str] = None,
    ) -> Optional[int]:
        """
        Match LC product to formula number
        Uses FormulaEngine as single source of truth for formula matching
        """
        return FormulaEngine.determine_formula(product_code, origin, quality, grade, product_name)

    @staticmethod
    def find_bulletin_for_lc(db: Session, lc_date: date, max_days: int = 14) -> Optional[int]:
        """
        Find the most appropriate bulletin for an LC based on LC date.
        Returns the most recent bulletin on or before the LC date, within max_days.
        If no bulletin exists within the lookback window, returns None — never silently
        uses a stale bulletin from a previous month.
        """
        from datetime import timedelta
        min_date = lc_date - timedelta(days=max_days)

        query = text("""
            SELECT bulletin_id
            FROM lme_bulletins
            WHERE bulletin_date <= :lc_date
              AND bulletin_date >= :min_date
            ORDER BY bulletin_date DESC
            LIMIT 1
        """)

        result = db.execute(query, {"lc_date": lc_date, "min_date": min_date}).fetchone()
        return result[0] if result else None

    @staticmethod
    def get_currency_rates(db: Session, bulletin_id: int) -> Tuple[Decimal, Decimal]:
        """
        Get USD and EUR rates from bulletin
        Returns (usd_rate, eur_rate) tuple
        """
        query = text("""
            SELECT cr.usd_rate, cr.eur_rate
            FROM lme_bulletins lb
            JOIN currency_rates cr ON lb.rate_id = cr.rate_id
            WHERE lb.bulletin_id = :bulletin_id
        """)

        result = db.execute(query, {"bulletin_id": bulletin_id}).fetchone()

        if result and result[0] and result[1]:
            return (Decimal(str(result[0])), Decimal(str(result[1])))

        # Fallback rates if not found
        return (Decimal("280"), Decimal("336"))

    @staticmethod
    def get_price_data(db: Session, bulletin_id: int, symbols: List[str]) -> Dict[str, Tuple[Decimal, Decimal]]:
        """
        Get price data for given symbols from a bulletin
        Returns dict: {symbol: (low_price, high_price)}
        """
        if not symbols:
            return {}

        placeholders = ', '.join([f':symbol_{i}' for i in range(len(symbols))])
        query = text(f"""
            SELECT symbol, low_price, high_price
            FROM lme_price_history
            WHERE bulletin_id = :bulletin_id
            AND symbol IN ({placeholders})
        """)

        params = {"bulletin_id": bulletin_id}
        for i, symbol in enumerate(symbols):
            params[f'symbol_{i}'] = symbol

        results = db.execute(query, params).fetchall()

        prices = {}
        for row in results:
            symbol, low, high = row
            if low is not None and high is not None:
                prices[symbol] = (Decimal(str(low)), Decimal(str(high)))

        return prices

    # ========== FORMULA CALCULATIONS ==========

    @staticmethod
    def calculate_formula_1(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 1: China PRIME (HRP/CRP/PPGI/GPP/GLP/GP) - LME = Average × 0.95 + 35"""
        symbol = None
        if product_code == 'HRP':
            symbol = 'MB-STE-0144'
        elif product_code == 'CRP':
            symbol = 'MB-STE-0145'
        elif product_code in ['PPGI', 'PPGIP', 'GPP', 'GLP', 'GP']:
            symbol = 'MB-STE-0009'

        if not symbol or symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        return avg * Decimal("0.95") + Decimal("35")

    @staticmethod
    def calculate_formula_2(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 2: China SECONDARY (HRS/CRS/GPS/GLS/PPGIS) - LME = Average × 0.85 + 45"""
        symbol = None
        if product_code == 'HRS':
            symbol = 'MB-STE-0144'
        elif product_code == 'CRS':
            symbol = 'MB-STE-0145'
        elif product_code in ['GPS', 'GLS', 'PPGIS']:
            symbol = 'MB-STE-0009'

        if not symbol or symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        return avg * Decimal("0.85") + Decimal("45")

    @staticmethod
    def calculate_formula_3(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 3: China SECONDARY (CRNGO) - LME = (Average × 1.05) × 0.85 + 45"""
        symbol = 'MB-STE-0145'

        if symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        after_add = avg * Decimal("1.05")
        return after_add * Decimal("0.85") + Decimal("45")

    @staticmethod
    def calculate_formula_4(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 4: China PRIME (WRLC) - LME = Average × 1.05 + 35"""
        symbol = 'MB-STE-0148'

        if symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        return avg * Decimal("1.05") + Decimal("35")

    @staticmethod
    def calculate_formula_5(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 5: China PRIME (WRHC) - LME = Average × 1.05 + 101"""
        symbol = 'MB-STE-0148'

        if symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        return avg * Decimal("1.05") + Decimal("101")

    @staticmethod
    def calculate_formula_6(prices: Dict[str, Tuple[Decimal, Decimal]],
                            usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 6: Europe SECONDARY (CRS/GPS) - LME = ((N+S)/2 / (USD/EUR)) × 0.85 + 100"""
        if product_code == 'CRS':
            north_symbol = 'MB-STE-0026'
            south_symbol = 'MB-STE-0027'
        elif product_code == 'GPS':
            north_symbol = 'MB-STE-0030'
            south_symbol = 'MB-STE-0031'
        else:
            raise ValueError(f"Invalid product for Formula 6: {product_code}")

        if north_symbol not in prices or south_symbol not in prices:
            raise ValueError(f"Prices not found for {product_code}")

        north_low, north_high = prices[north_symbol]
        south_low, south_high = prices[south_symbol]

        north_avg = (north_low + north_high) / 2
        south_avg = (south_low + south_high) / 2
        combined_eur = (north_avg + south_avg) / 2

        # EUR to USD conversion
        combined_usd = combined_eur / (usd_rate / eur_rate)

        return combined_usd * Decimal("0.85") + Decimal("100")

    @staticmethod
    def calculate_formula_7(prices: Dict[str, Tuple[Decimal, Decimal]],
                            usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 7: Europe SECONDARY (HRS) - LME = ((3-avg) / (USD/EUR)) × 0.85 + 100"""
        required_symbols = ['MB-STE-0028', 'MB-STE-0892', 'MB-STE-0893']

        if not all(s in prices for s in required_symbols):
            raise ValueError(f"Prices not found for {product_code}")

        north_low, north_high = prices['MB-STE-0028']
        italy_low, italy_high = prices['MB-STE-0892']
        spain_low, spain_high = prices['MB-STE-0893']

        north_avg = (north_low + north_high) / 2
        italy_avg = (italy_low + italy_high) / 2
        spain_avg = (spain_low + spain_high) / 2

        combined_eur = (north_avg + italy_avg + spain_avg) / 3
        combined_usd = combined_eur / (usd_rate / eur_rate)

        return combined_usd * Decimal("0.85") + Decimal("100")

    @staticmethod
    def calculate_formula_8(prices: Dict[str, Tuple[Decimal, Decimal]],
                            usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 8: S.Africa/Taiwan SECONDARY (CRS/GPS) - LME = (4-region avg) × 0.85 + 100"""
        if product_code == 'CRS':
            europe_symbols = ['MB-STE-0026', 'MB-STE-0027']
            uae_symbol = 'MB-STE-0124'
            usa_symbol = 'MB-STE-0181'
            china_symbol = 'MB-STE-0145'
        elif product_code == 'GPS':
            europe_symbols = ['MB-STE-0030', 'MB-STE-0031']
            uae_symbol = 'MB-STE-0123'
            usa_symbol = 'MB-STE-0182'
            china_symbol = 'MB-STE-0009'
        else:
            raise ValueError(f"Invalid product for Formula 8: {product_code}")

        # Europe average and convert
        europe_north_low, europe_north_high = prices[europe_symbols[0]]
        europe_south_low, europe_south_high = prices[europe_symbols[1]]
        europe_north = (europe_north_low + europe_north_high) / 2
        europe_south = (europe_south_low + europe_south_high) / 2
        europe_eur = (europe_north + europe_south) / 2
        europe_usd = europe_eur / (usd_rate / eur_rate)

        # Other regions
        uae_low, uae_high = prices[uae_symbol]
        uae_avg = (uae_low + uae_high) / 2

        usa_low, usa_high = prices[usa_symbol]
        usa_avg = (usa_low + usa_high) / 2

        china_low, china_high = prices[china_symbol]
        china_avg = (china_low + china_high) / 2

        four_region_avg = (europe_usd + uae_avg + usa_avg + china_avg) / 4

        return four_region_avg * Decimal("0.85") + Decimal("100")

    @staticmethod
    def calculate_formula_9(prices: Dict[str, Tuple[Decimal, Decimal]],
                            usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 9: S.Africa/Taiwan SECONDARY (HRS) - LME = (4-region avg) × 0.85 + 100"""
        required_symbols = ['MB-STE-0028', 'MB-STE-0892', 'MB-STE-0893',
                            'MB-STE-0014', 'MB-STE-0125', 'MB-STE-0144']

        if not all(s in prices for s in required_symbols):
            raise ValueError(f"Prices not found for {product_code}")

        # Europe: 3-source average
        north_low, north_high = prices['MB-STE-0028']
        italy_low, italy_high = prices['MB-STE-0892']
        spain_low, spain_high = prices['MB-STE-0893']

        north_avg = (north_low + north_high) / 2
        italy_avg = (italy_low + italy_high) / 2
        spain_avg = (spain_low + spain_high) / 2

        europe_eur = (north_avg + italy_avg + spain_avg) / 3
        europe_usd = europe_eur / (usd_rate / eur_rate)

        # Other regions
        cis_low, cis_high = prices['MB-STE-0014']
        cis_avg = (cis_low + cis_high) / 2

        uae_low, uae_high = prices['MB-STE-0125']
        uae_avg = (uae_low + uae_high) / 2

        china_low, china_high = prices['MB-STE-0144']
        china_avg = (china_low + china_high) / 2

        four_region_avg = (europe_usd + cis_avg + uae_avg + china_avg) / 4

        return four_region_avg * Decimal("0.85") + Decimal("100")

    @staticmethod
    def calculate_formula_10(prices: Dict[str, Tuple[Decimal, Decimal]],
                             usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 10: UAE/Iran PRIME (WRLC) - LME = (4-region avg) × 1.05 + 35"""
        required_symbols = ['MB-STE-0053', 'MB-STE-0054', 'MB-STE-0017',
                            'MB-STE-0120', 'MB-STE-0148']

        if not all(s in prices for s in required_symbols):
            raise ValueError(f"Prices not found for {product_code}")

        # Europe average and convert
        north_low, north_high = prices['MB-STE-0053']
        south_low, south_high = prices['MB-STE-0054']

        europe_north = (north_low + north_high) / 2
        europe_south = (south_low + south_high) / 2
        europe_eur = (europe_north + europe_south) / 2
        europe_usd = europe_eur / (usd_rate / eur_rate)

        # Other regions
        cis_low, cis_high = prices['MB-STE-0017']
        cis_avg = (cis_low + cis_high) / 2

        turkish_low, turkish_high = prices['MB-STE-0120']
        turkish_avg = (turkish_low + turkish_high) / 2

        china_low, china_high = prices['MB-STE-0148']
        china_avg = (china_low + china_high) / 2

        four_region_avg = (europe_usd + cis_avg + turkish_avg + china_avg) / 4

        return four_region_avg * Decimal("1.05") + Decimal("35")

    @staticmethod
    def calculate_formula_11(prices: Dict[str, Tuple[Decimal, Decimal]],
                             usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Decimal:
        """Formula 11: UAE/Iran PRIME (WRHC) - LME = (4-region avg) × 1.05 + 101"""
        required_symbols = ['MB-STE-0053', 'MB-STE-0054', 'MB-STE-0017',
                            'MB-STE-0120', 'MB-STE-0148']

        if not all(s in prices for s in required_symbols):
            raise ValueError(f"Prices not found for {product_code}")

        # Europe average and convert
        north_low, north_high = prices['MB-STE-0053']
        south_low, south_high = prices['MB-STE-0054']

        europe_north = (north_low + north_high) / 2
        europe_south = (south_low + south_high) / 2
        europe_eur = (europe_north + europe_south) / 2
        europe_usd = europe_eur / (usd_rate / eur_rate)

        # Other regions
        cis_low, cis_high = prices['MB-STE-0017']
        cis_avg = (cis_low + cis_high) / 2

        turkish_low, turkish_high = prices['MB-STE-0120']
        turkish_avg = (turkish_low + turkish_high) / 2

        china_low, china_high = prices['MB-STE-0148']
        china_avg = (china_low + china_high) / 2

        four_region_avg = (europe_usd + cis_avg + turkish_avg + china_avg) / 4

        return four_region_avg * Decimal("1.05") + Decimal("101")

    @staticmethod
    def calculate_formula_12(prices: Dict[str, Tuple[Decimal, Decimal]], product_code: str) -> Decimal:
        """Formula 12: Plastic (PMC) - LME = Average × factor + premium
        TODO: factor(1.0)/premium(0) are placeholders — confirm real PMC constants."""
        symbol = 'MB-PMC-0001'

        if symbol not in prices:
            raise ValueError(f"Price not found for {product_code}")

        low, high = prices[symbol]
        avg = (low + high) / 2
        return avg * Decimal("1.0") + Decimal("0")

    @staticmethod
    def _apply_formula(formula_number: int, prices: Dict[str, Tuple[Decimal, Decimal]],
                       usd_rate: Decimal, eur_rate: Decimal, product_code: str) -> Optional[Decimal]:
        """Single dispatch table shared by calculate_single_lc() and calculate_batch() so
        the two can never drift apart on which formula function handles which number."""
        if formula_number == 1:
            return LMECalculator.calculate_formula_1(prices, product_code)
        elif formula_number == 2:
            return LMECalculator.calculate_formula_2(prices, product_code)
        elif formula_number == 3:
            return LMECalculator.calculate_formula_3(prices, product_code)
        elif formula_number == 4:
            return LMECalculator.calculate_formula_4(prices, product_code)
        elif formula_number == 5:
            return LMECalculator.calculate_formula_5(prices, product_code)
        elif formula_number == 6:
            return LMECalculator.calculate_formula_6(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 7:
            return LMECalculator.calculate_formula_7(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 8:
            return LMECalculator.calculate_formula_8(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 9:
            return LMECalculator.calculate_formula_9(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 10:
            return LMECalculator.calculate_formula_10(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 11:
            return LMECalculator.calculate_formula_11(prices, usd_rate, eur_rate, product_code)
        elif formula_number == 12:
            return LMECalculator.calculate_formula_12(prices, product_code)
        return None

    @staticmethod
    def calculate_single_lc(db: Session, lc_id: int, bulletin_id: int = None) -> Dict:
        """
        Calculate LME for a single LC.
        If bulletin_id is provided, use that specific bulletin instead of auto-finding one.
        Returns complete calculation result with all details.
        """
        # Get LC details
        lc_query = text("""
            SELECT
                lm.lc_id, lm.lc_number, lm.lc_date,
                lp.product_code, lp.origin, lp.quality, lp.lc_unit_price,
                lp.grade, lp.product_name
            FROM lc_master lm
            JOIN lc_products lp ON lm.lc_id = lp.lc_id
            WHERE lm.lc_id = :lc_id
            LIMIT 1
        """)

        lc_result = db.execute(lc_query, {"lc_id": lc_id}).fetchone()

        if not lc_result:
            raise ValueError(f"LC {lc_id} not found")

        lc_id, lc_number, lc_date, product_code, origin, quality, lc_unit_price, grade, product_name = lc_result

        # Match formula
        formula_number = LMECalculator.match_formula(origin, quality, product_code, grade, product_name)

        if not formula_number:
            raise ValueError(
                f"Could not match formula for LC {lc_number} "
                f"(product_code={product_code!r}, origin={origin!r}, quality={quality!r}) — "
                f"this combination isn't mapped to any formula. Check that origin/quality are set "
                f"and spelled as expected (origin must contain one of CHINA/EUROPE/GERMANY/ITALY/"
                f"SPAIN/NETHERLANDS/TAIWAN/AFRICA/UAE/IRAN; quality must be PRIME or SECONDARY)."
            )

        # Use provided bulletin_id or find appropriate one based on LC date
        if bulletin_id is None:
            bulletin_id = LMECalculator.find_bulletin_for_lc(db, lc_date)

        if not bulletin_id:
            raise ValueError(f"No bulletin available within 14 days before LC date {lc_date} — upload the bulletin first")

        # Get required symbols
        symbols = LMECalculator.get_symbols_for_product(formula_number, product_code)

        if not symbols:
            raise ValueError(f"No symbols found for formula {formula_number} and product {product_code}")

        # Get price data
        prices = LMECalculator.get_price_data(db, bulletin_id, symbols)

        if not prices:
            raise ValueError(f"No prices found for LC {lc_number}")

        # Get currency rates (needed for formulas 6-11)
        usd_rate, eur_rate = LMECalculator.get_currency_rates(db, bulletin_id)

        # Calculate based on formula
        lme_value = LMECalculator._apply_formula(formula_number, prices, usd_rate, eur_rate, product_code)

        if lme_value is None:
            raise ValueError(f"Failed to calculate LME for formula {formula_number}")

        return {
            "lc_id": lc_id,
            "lc_number": lc_number,
            "formula_number": formula_number,
            "bulletin_id": bulletin_id,
            "lme_value": float(lme_value),
            "product_code": product_code,
            "origin": origin,
            "quality": quality,
            "symbols_used": symbols
        }

    @staticmethod
    def calculate_batch(db: Session, lc_ids: List[int], bulletin_id: int = None) -> Dict[int, Dict]:
        """Batched equivalent of calling calculate_single_lc() once per LC in a loop.

        calculate_single_lc() does up to 4 sequential DB round trips per call (the LC row,
        bulletin lookup, price data, currency rates) — fine for one LC, but calculate_all_lcs/
        bulletin_impact/apply_rates call it once per LC in a loop, so a 40-day bulletin-impact
        batch of a few hundred LCs turned into 1000+ sequential queries even though most of
        those LCs share the same handful of bulletins. This fetches each piece of reference
        data ONCE for the whole batch and reuses it, then runs the exact same formula
        functions (via the shared _apply_formula dispatch) with zero DB calls per LC.

        Returns {lc_id: result} where result matches calculate_single_lc()'s return dict on
        success, or {"error": str(...)} on failure for that LC — mirroring the try/except-per-LC
        behavior the callers already had around calculate_single_lc().
        """
        if not lc_ids:
            return {}

        # 1. Every LC's product/pricing row in one query instead of one query per LC.
        #    calculate_single_lc()'s JOIN + LIMIT 1 has no ORDER BY (arbitrary pick among
        #    multiple product rows) — keeping the first row seen per lc_id here preserves
        #    that same "undefined which one" behavior rather than introducing a new tiebreak.
        placeholders = ", ".join(f":lc_id_{i}" for i in range(len(lc_ids)))
        params = {f"lc_id_{i}": lid for i, lid in enumerate(lc_ids)}
        lc_rows = db.execute(text(f"""
            SELECT lm.lc_id, lm.lc_number, lm.lc_date, lp.product_code, lp.origin, lp.quality,
                   lp.grade, lp.product_name
            FROM lc_master lm
            JOIN lc_products lp ON lm.lc_id = lp.lc_id
            WHERE lm.lc_id IN ({placeholders})
        """), params).fetchall()

        lc_data: Dict[int, Dict] = {}
        for row in lc_rows:
            lid = row[0]
            if lid not in lc_data:
                lc_data[lid] = {
                    "lc_number": row[1], "lc_date": row[2],
                    "product_code": row[3], "origin": row[4], "quality": row[5],
                    "grade": row[6], "product_name": row[7],
                }

        # 2. Resolve each LC's bulletin. A fixed bulletin_id (bulletin_impact/apply_rates)
        #    applies to every LC with no lookup needed. Otherwise, replicate
        #    find_bulletin_for_lc()'s "latest bulletin_date <= lc_date, within 14 days"
        #    rule in Python against ONE pre-fetched, sorted bulletin list instead of one
        #    query per LC's own lc_date.
        if bulletin_id is not None:
            bulletin_for = {lid: bulletin_id for lid in lc_data}
        else:
            all_bulletins = db.execute(text(
                "SELECT bulletin_id, bulletin_date FROM lme_bulletins ORDER BY bulletin_date ASC"
            )).fetchall()
            bulletin_dates = [b[1] for b in all_bulletins]
            bulletin_ids_sorted = [b[0] for b in all_bulletins]
            bulletin_for = {}
            for lid, d in lc_data.items():
                lc_date = d["lc_date"]
                idx = bisect.bisect_right(bulletin_dates, lc_date) - 1
                if idx >= 0 and (lc_date - bulletin_dates[idx]).days <= 14:
                    bulletin_for[lid] = bulletin_ids_sorted[idx]
                else:
                    bulletin_for[lid] = None

        # 3. Formula + required symbols per LC — pure functions, zero DB access.
        formula_for = {}
        symbols_for = {}
        for lid, d in lc_data.items():
            fnum = LMECalculator.match_formula(d["origin"], d["quality"], d["product_code"], d["grade"], d["product_name"])
            formula_for[lid] = fnum
            symbols_for[lid] = LMECalculator.get_symbols_for_product(fnum, d["product_code"]) if fnum else []

        needed_bulletins = sorted({b for b in bulletin_for.values() if b is not None})

        # 4. Currency rates for every distinct bulletin used — one query instead of one per LC.
        rates_by_bulletin: Dict[int, Tuple[Decimal, Decimal]] = {}
        if needed_bulletins:
            ph = ", ".join(f":b_{i}" for i in range(len(needed_bulletins)))
            p = {f"b_{i}": b for i, b in enumerate(needed_bulletins)}
            for bid, usd, eur in db.execute(text(f"""
                SELECT lb.bulletin_id, cr.usd_rate, cr.eur_rate
                FROM lme_bulletins lb
                JOIN currency_rates cr ON lb.rate_id = cr.rate_id
                WHERE lb.bulletin_id IN ({ph})
            """), p).fetchall():
                if usd is not None and eur is not None:
                    rates_by_bulletin[bid] = (Decimal(str(usd)), Decimal(str(eur)))

        # 5. Prices for every distinct bulletin used — one query instead of one per LC
        #    (fetches every symbol on the bulletin rather than filtering to each LC's
        #    specific symbols; a bulletin's price sheet is a small, bounded set, so this
        #    is cheap and lets every LC on that bulletin share the same fetch).
        prices_by_bulletin: Dict[int, Dict[str, Tuple[Decimal, Decimal]]] = {}
        if needed_bulletins:
            ph = ", ".join(f":b_{i}" for i in range(len(needed_bulletins)))
            p = {f"b_{i}": b for i, b in enumerate(needed_bulletins)}
            for bid, symbol, low, high in db.execute(text(f"""
                SELECT bulletin_id, symbol, low_price, high_price
                FROM lme_price_history
                WHERE bulletin_id IN ({ph})
            """), p).fetchall():
                if low is not None and high is not None:
                    prices_by_bulletin.setdefault(bid, {})[symbol] = (Decimal(str(low)), Decimal(str(high)))

        # 6. Calculate — same formula functions as calculate_single_lc(), no DB calls here.
        results: Dict[int, Dict] = {}
        for lc_id in lc_ids:
            try:
                if lc_id not in lc_data:
                    raise ValueError(f"LC {lc_id} not found")
                d = lc_data[lc_id]
                product_code, origin, quality = d["product_code"], d["origin"], d["quality"]

                formula_number = formula_for[lc_id]
                if not formula_number:
                    raise ValueError(
                        f"Could not match formula for LC {d['lc_number']} "
                        f"(product_code={product_code!r}, origin={origin!r}, quality={quality!r}) — "
                        f"this combination isn't mapped to any formula."
                    )

                bid = bulletin_for[lc_id]
                if not bid:
                    raise ValueError(
                        f"No bulletin available within 14 days before LC date {d['lc_date']} — "
                        f"upload the bulletin first")

                symbols = symbols_for[lc_id]
                if not symbols:
                    raise ValueError(f"No symbols found for formula {formula_number} and product {product_code}")

                prices = prices_by_bulletin.get(bid, {})
                if not prices:
                    raise ValueError(f"No prices found for LC {d['lc_number']}")

                usd_rate, eur_rate = rates_by_bulletin.get(bid, (Decimal("280"), Decimal("336")))

                lme_value = LMECalculator._apply_formula(formula_number, prices, usd_rate, eur_rate, product_code)
                if lme_value is None:
                    raise ValueError(f"Failed to calculate LME for formula {formula_number}")

                results[lc_id] = {
                    "lc_id": lc_id, "lc_number": d["lc_number"], "formula_number": formula_number,
                    "bulletin_id": bid, "lme_value": float(lme_value),
                    "product_code": product_code, "origin": origin, "quality": quality,
                    "symbols_used": symbols,
                }
            except Exception as e:
                results[lc_id] = {"error": str(e)}

        return results
