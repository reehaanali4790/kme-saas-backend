"""
LME Monitoring System - Formula Engine
Version: 2.0
All 11 LME calculation formulas (CORRECTED)
"""

from decimal import Decimal
from typing import Dict, Optional, Tuple
import re
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Products that must NEVER be priced by the steel formula set — they are either not
# LME-quoted at all (SCRAP, FLUX are recorded but not rate-calculated) or intentionally
# out of scope for LME calculation per business rule. Matched by substring against the
# uppercased product code / item code, so aliases (e.g. "SILICOMANGANESE" vs
# "SILL MAGNEESE") are both caught.
LME_EXCLUDED_PRODUCTS = ("SCRAP", "EG", "FLUX", "SILL MAGNEESE", "SILICOMANGANESE", "PUPHRS", "PMC")

# China-origin steel codes recognized by Formula 1 (PRIME) / Formula 2 (SECONDARY) —
# used to stop the SECONDARY branch from acting as a catch-all for any unrecognized
# code (that used to silently route e.g. an excluded/unknown product into Formula 2).
_CHINA_PRIME_CODES = ('HRP', 'CRP', 'PPGI', 'PPGIP', 'GPP', 'GLP', 'GP')
_CHINA_SECONDARY_CODES = ('HRS', 'CRS', 'GPS', 'GLS', 'PPGIS')


def is_lme_excluded_product(product_code: Optional[str]) -> bool:
    """True when a product must be skipped entirely by LME calculation (never priced,
    not even a fallback formula)."""
    code = (product_code or "").upper()
    return any(excluded in code for excluded in LME_EXCLUDED_PRODUCTS)


class FormulaEngine:
    """LME Price Calculation Engine"""

    # EUR/USD rates (will be updated from PDF or manual input)
    eur_rate: Decimal = Decimal("330.96")
    usd_rate: Decimal = Decimal("280.2")

    @classmethod
    def set_eur_usd_rates(cls, eur_rate: Decimal, usd_rate: Decimal):
        """Set EUR and USD rates for conversion"""
        cls.eur_rate = eur_rate
        cls.usd_rate = usd_rate

    @classmethod
    def eur_to_usd(cls, eur_value: Decimal) -> Decimal:
        """
        Convert EUR to USD using PKR rates.
        Formula: EUR * (EUR_PKR / USD_PKR) — consistent with lme_calculator.py
        """
        conversion_rate = cls.eur_rate / cls.usd_rate
        return eur_value * conversion_rate

    @staticmethod
    def calculate_average(low: Decimal, high: Decimal) -> Decimal:
        """Calculate average of low and high"""
        return (low + high) / 2

    @staticmethod
    def determine_formula(
        product_code: str, origin: str, quality: str,
        grade: Optional[str] = None, product_name: Optional[str] = None,
    ) -> Optional[int]:
        """
        Determine which formula to use for LME calculation
        Returns formula number (1-12), or None when the product/origin/quality
        combination has no matching formula.

        THIS IS THE SINGLE SOURCE OF TRUTH FOR FORMULA MATCHING.
        All other files should import and use this function.

        Args:
            product_code: Product code (e.g., 'HRP', 'CRS', 'GPS')
            origin: Origin country (e.g., 'CHINA', 'NETHERLANDS', 'UAE')
            quality: Kept for API/call-site stability, but NOT used to route the match —
                every product code below is already self-describing prime-vs-secondary by
                its own P/S suffix (GPP vs GPS, CRP vs CRS, HRP vs HRS, ...) and the two
                sets never overlap, so this field can only ever conflict with what the code
                already says (and in real LC data, it has — CRNGO/GPS lines filed with
                quality=PRIME used to wrongly fall through to "no formula"). The code wins.
            grade: Optional free-text grade field off the LC line (e.g. "High Carbon") —
                used only to dynamically resolve a bare "WR" (wire rod, no carbon grade
                in the code itself) into WRLC/WRHC; ignored for every other product.
            product_name: Optional raw goods-description text (LC 45A field) — same use
                as `grade`, checked as a second source when `grade` doesn't say either way.

        Returns:
            Formula number (1-12), or None if no formula applies — callers must treat
            None as "do not calculate" (excluded product, or a combination that's never
            been mapped to a formula) rather than silently falling back to Formula 1.
        """
        code = (product_code or "").upper()

        # Never price excluded products (SCRAP, EG, FLUX, SILL MAGNEESE/SILICOMANGANESE,
        # PUPHRS) — checked before any origin/quality routing so nothing downstream can
        # accidentally match them into a steel formula.
        if is_lme_excluded_product(code):
            return None

        # Plastic (PMC) — its own formula, independent of the steel origin/quality matrix.
        if code == 'PMC':
            return 12

        # Bare "WR" (wire rod recorded without a carbon grade in the product code itself)
        # — resolve dynamically from the LC line's own grade/description text rather than
        # guessing a fixed default, since Low vs High Carbon changes the freight premium
        # ($35 vs $101). Rewriting `code` here lets it flow through the existing WRLC/WRHC
        # branches below (both China and UAE/Iran) unchanged. Falls through to "no match"
        # (as before) when neither field actually states a carbon grade — still never
        # silently mis-priced.
        if code == 'WR':
            detail_text = f"{grade or ''} {product_name or ''}".upper()
            if re.search(r'HIGH[\s-]*CARBON|\bHC\b', detail_text):
                code = 'WRHC'
            elif re.search(r'LOW[\s-]*CARBON|\bLC\b', detail_text):
                code = 'WRLC'

        origin_upper = origin.upper() if origin else ""

        # China origin
        # NOTE: matched on product code alone, NOT gated by the `quality` field. Every
        # code below is already self-describing prime-vs-secondary by its own P/S suffix
        # (GPP=Galvanized Plain Prime vs GPS=...Secondary, CRP vs CRS, HRP vs HRS, etc.)
        # and the two code sets never overlap, so `quality` carries no extra information
        # here — it can only ever conflict with what the code already says. It has, in
        # practice: real LC data has both CRNGO and GPS (both secondary-only codes) filed
        # with quality=PRIME, which used to make them fall through to "no formula" even
        # though the code alone fully determines the answer. Trust the code.
        if 'CHINA' in origin_upper:
            if code == 'CRNGO':
                return 3
            if code in _CHINA_PRIME_CODES:
                return 1
            if code in _CHINA_SECONDARY_CODES:
                return 2
            if code == 'WRLC':
                return 4
            if code == 'WRHC':
                return 5

        # Europe origin - INCLUDES NETHERLANDS FIX
        elif ('EUROPE' in origin_upper or
              'GERMAN' in origin_upper or
              'ITALY' in origin_upper or
              'SPAIN' in origin_upper or
              'NETHERLAND' in origin_upper):  # FIXED: Added Netherlands
            if code in ['CRS', 'GPS']:
                return 6
            elif code == 'HRS':
                return 7

        # Taiwan / South Africa origin
        elif 'TAIWAN' in origin_upper or 'AFRICA' in origin_upper:
            if code in ['CRS', 'GPS']:
                return 8
            elif code == 'HRS':
                return 9

        # UAE / Iran origin
        elif 'UAE' in origin_upper or 'IRAN' in origin_upper:
            if code == 'WRLC':
                return 10
            elif code == 'WRHC':
                return 11
            elif code == 'HRP':
                return 1  # UAE HRP uses Formula 1 (HRP is unambiguously prime-grade by code alone)

        # No match — do NOT default to Formula 1. An unmapped product/origin/quality
        # combination must surface as "could not match formula", not get silently
        # mis-priced as China-Prime-HRP.
        return None

    @staticmethod
    def formula_1(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 1: China/UAE · Region 1 · PRIME (HRP, CRP, PPGI, GPP, GLP, GP)
        CORRECTED: Discount is -5% (not +5%)
        LME = Average × 0.95 + 35
        """
        avg = FormulaEngine.calculate_average(low, high)
        lme = avg * Decimal("0.95") + Decimal("35")

        return {
            "formula_number": 1,
            "average": float(avg),
            "discount_multiplier": 0.95,
            "freight": 35,
            "lme": float(lme),
            "note": "CORRECTED: -5% discount"
        }

    @staticmethod
    def formula_2(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 2: China · Region 1 · SECONDARY (HRS, CRS, GPS, GLS, PPGIS)
        LME = Average × 0.85 + 45
        """
        avg = FormulaEngine.calculate_average(low, high)
        lme = avg * Decimal("0.85") + Decimal("45")

        return {
            "formula_number": 2,
            "average": float(avg),
            "discount_multiplier": 0.85,
            "freight": 45,
            "lme": float(lme)
        }

    @staticmethod
    def formula_3(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 3: China · Region 1 · SECONDARY (CRNGO)
        CORRECTED: Add 5% first, then apply -15% discount
        LME = (Average × 1.05) × 0.85 + 45
        """
        avg = FormulaEngine.calculate_average(low, high)
        after_add = avg * Decimal("1.05")
        lme = after_add * Decimal("0.85") + Decimal("45")

        return {
            "formula_number": 3,
            "average": float(avg),
            "after_5_percent_add": float(after_add),
            "discount_multiplier": 0.85,
            "freight": 45,
            "lme": float(lme),
            "note": "CORRECTED: +5% then -15%"
        }

    @staticmethod
    def formula_4(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 4: China · Region 1 · PRIME (WRLC - Wire Rod Low Carbon)
        LME = Average × 1.05 + 35
        """
        avg = FormulaEngine.calculate_average(low, high)
        lme = avg * Decimal("1.05") + Decimal("35")

        return {
            "formula_number": 4,
            "average": float(avg),
            "premium_multiplier": 1.05,
            "freight": 35,
            "lme": float(lme)
        }

    @staticmethod
    def formula_5(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 5: China · Region 1 · PRIME (WRHC - Wire Rod High Carbon)
        LME = Average × 1.05 + 101 (35 + 66 extra for HC)
        """
        avg = FormulaEngine.calculate_average(low, high)
        lme = avg * Decimal("1.05") + Decimal("101")

        return {
            "formula_number": 5,
            "average": float(avg),
            "premium_multiplier": 1.05,
            "freight": 101,
            "lme": float(lme),
            "note": "Extra +66 freight for High Carbon"
        }

    @classmethod
    def formula_6(cls, north_low: Decimal, north_high: Decimal,
                   south_low: Decimal, south_high: Decimal) -> Dict:
        """
        Formula 6: Europe · Region 2 · SECONDARY (CRS, GPS)
        CORRECTED: EUR→USD conversion
        LME = ((North + South) / 2 / (USD/EUR)) × 0.85 + 100
        """
        north_avg = cls.calculate_average(north_low, north_high)
        south_avg = cls.calculate_average(south_low, south_high)
        combined_eur = (north_avg + south_avg) / 2

        # CORRECTED: Division not multiplication, USD/EUR not EUR/USD
        combined_usd = combined_eur / (cls.usd_rate / cls.eur_rate)

        lme = combined_usd * Decimal("0.85") + Decimal("100")

        return {
            "formula_number": 6,
            "north_avg": float(north_avg),
            "south_avg": float(south_avg),
            "combined_eur": float(combined_eur),
            "combined_usd": float(combined_usd),
            "discount_multiplier": 0.85,
            "freight": 100,
            "lme": float(lme),
            "note": "CORRECTED: EUR÷(USD/EUR) conversion"
        }

    @classmethod
    def formula_7(cls, europe_north_low: Decimal, europe_north_high: Decimal,
                   italy_low: Decimal, italy_high: Decimal,
                   spain_low: Decimal, spain_high: Decimal) -> Dict:
        """
        Formula 7: Europe · Region 2A · SECONDARY (HRS)
        3 sources: North Europe, Italy, Spain
        LME = ((3-source avg) / (USD/EUR)) × 0.85 + 100
        """
        north_avg = cls.calculate_average(europe_north_low, europe_north_high)
        italy_avg = cls.calculate_average(italy_low, italy_high)
        spain_avg = cls.calculate_average(spain_low, spain_high)

        combined_eur = (north_avg + italy_avg + spain_avg) / 3
        combined_usd = combined_eur / (cls.usd_rate / cls.eur_rate)

        lme = combined_usd * Decimal("0.85") + Decimal("100")

        return {
            "formula_number": 7,
            "north_avg": float(north_avg),
            "italy_avg": float(italy_avg),
            "spain_avg": float(spain_avg),
            "combined_eur": float(combined_eur),
            "combined_usd": float(combined_usd),
            "discount_multiplier": 0.85,
            "freight": 100,
            "lme": float(lme)
        }

    @classmethod
    def formula_8(cls, europe_north_low: Decimal, europe_north_high: Decimal,
                   europe_south_low: Decimal, europe_south_high: Decimal,
                   uae_low: Decimal, uae_high: Decimal,
                   usa_low: Decimal, usa_high: Decimal,
                   china_low: Decimal, china_high: Decimal) -> Dict:
        """
        Formula 8: S.Africa/Taiwan · Region 4A · SECONDARY (CRS, GPS)
        4 regions: Europe (N+S), UAE, USA, China
        LME = (4-region avg) × 0.85 + 100
        """
        # Europe average and convert
        europe_north = cls.calculate_average(europe_north_low, europe_north_high)
        europe_south = cls.calculate_average(europe_south_low, europe_south_high)
        europe_combined_eur = (europe_north + europe_south) / 2
        europe_usd = europe_combined_eur / (cls.usd_rate / cls.eur_rate)

        # Other regions (already in USD)
        uae_avg = cls.calculate_average(uae_low, uae_high)
        usa_avg = cls.calculate_average(usa_low, usa_high)
        china_avg = cls.calculate_average(china_low, china_high)

        # 4-region average
        four_region_avg = (europe_usd + uae_avg + usa_avg + china_avg) / 4

        lme = four_region_avg * Decimal("0.85") + Decimal("100")

        return {
            "formula_number": 8,
            "europe_usd": float(europe_usd),
            "uae_avg": float(uae_avg),
            "usa_avg": float(usa_avg),
            "china_avg": float(china_avg),
            "four_region_avg": float(four_region_avg),
            "discount_multiplier": 0.85,
            "freight": 100,
            "lme": float(lme)
        }

    @classmethod
    def formula_9(cls, europe_north_low: Decimal, europe_north_high: Decimal,
                   italy_low: Decimal, italy_high: Decimal,
                   spain_low: Decimal, spain_high: Decimal,
                   cis_low: Decimal, cis_high: Decimal,
                   uae_low: Decimal, uae_high: Decimal,
                   china_low: Decimal, china_high: Decimal) -> Dict:
        """
        Formula 9: S.Africa/Taiwan · Region 4B · SECONDARY (HRS)
        6 sources = 4 regions: Europe (3-source), CIS, UAE, China
        LME = (4-region avg) × 0.85 + 100
        """
        # Europe: average of 3 sources
        north_avg = cls.calculate_average(europe_north_low, europe_north_high)
        italy_avg = cls.calculate_average(italy_low, italy_high)
        spain_avg = cls.calculate_average(spain_low, spain_high)
        europe_combined_eur = (north_avg + italy_avg + spain_avg) / 3
        europe_usd = europe_combined_eur / (cls.usd_rate / cls.eur_rate)

        # Other regions (already in USD)
        cis_avg = cls.calculate_average(cis_low, cis_high)
        uae_avg = cls.calculate_average(uae_low, uae_high)
        china_avg = cls.calculate_average(china_low, china_high)

        # 4-region average
        four_region_avg = (europe_usd + cis_avg + uae_avg + china_avg) / 4

        lme = four_region_avg * Decimal("0.85") + Decimal("100")

        return {
            "formula_number": 9,
            "europe_usd": float(europe_usd),
            "cis_avg": float(cis_avg),
            "uae_avg": float(uae_avg),
            "china_avg": float(china_avg),
            "four_region_avg": float(four_region_avg),
            "discount_multiplier": 0.85,
            "freight": 100,
            "lme": float(lme)
        }

    @classmethod
    def formula_10(cls, europe_north_low: Decimal, europe_north_high: Decimal,
                    europe_south_low: Decimal, europe_south_high: Decimal,
                    cis_low: Decimal, cis_high: Decimal,
                    turkish_low: Decimal, turkish_high: Decimal,
                    china_low: Decimal, china_high: Decimal) -> Dict:
        """
        Formula 10: UAE/Iran · Region 4C · PRIME (WRLC)
        5 sources = 4 regions: Europe (N+S), CIS, Turkish, China
        LME = (4-region avg) × 1.05 + 35
        """
        # Europe average and convert
        europe_north = cls.calculate_average(europe_north_low, europe_north_high)
        europe_south = cls.calculate_average(europe_south_low, europe_south_high)
        europe_combined_eur = (europe_north + europe_south) / 2
        europe_usd = europe_combined_eur / (cls.usd_rate / cls.eur_rate)

        # Other regions (already in USD)
        cis_avg = cls.calculate_average(cis_low, cis_high)
        turkish_avg = cls.calculate_average(turkish_low, turkish_high)
        china_avg = cls.calculate_average(china_low, china_high)

        # 4-region average
        four_region_avg = (europe_usd + cis_avg + turkish_avg + china_avg) / 4

        lme = four_region_avg * Decimal("1.05") + Decimal("35")

        return {
            "formula_number": 10,
            "europe_usd": float(europe_usd),
            "cis_avg": float(cis_avg),
            "turkish_avg": float(turkish_avg),
            "china_avg": float(china_avg),
            "four_region_avg": float(four_region_avg),
            "premium_multiplier": 1.05,
            "freight": 35,
            "lme": float(lme)
        }

    @classmethod
    def formula_11(cls, europe_north_low: Decimal, europe_north_high: Decimal,
                    europe_south_low: Decimal, europe_south_high: Decimal,
                    cis_low: Decimal, cis_high: Decimal,
                    turkish_low: Decimal, turkish_high: Decimal,
                    china_low: Decimal, china_high: Decimal) -> Dict:
        """
        Formula 11: UAE/Iran · Region 4C · PRIME (WRHC)
        5 sources = 4 regions: Europe (N+S), CIS, Turkish, China
        LME = (4-region avg) × 1.05 + 101 (35 + 66 extra for HC)
        """
        # Europe average and convert
        europe_north = cls.calculate_average(europe_north_low, europe_north_high)
        europe_south = cls.calculate_average(europe_south_low, europe_south_high)
        europe_combined_eur = (europe_north + europe_south) / 2
        europe_usd = europe_combined_eur / (cls.usd_rate / cls.eur_rate)

        # Other regions (already in USD)
        cis_avg = cls.calculate_average(cis_low, cis_high)
        turkish_avg = cls.calculate_average(turkish_low, turkish_high)
        china_avg = cls.calculate_average(china_low, china_high)

        # 4-region average
        four_region_avg = (europe_usd + cis_avg + turkish_avg + china_avg) / 4

        lme = four_region_avg * Decimal("1.05") + Decimal("101")

        return {
            "formula_number": 11,
            "europe_usd": float(europe_usd),
            "cis_avg": float(cis_avg),
            "turkish_avg": float(turkish_avg),
            "china_avg": float(china_avg),
            "four_region_avg": float(four_region_avg),
            "premium_multiplier": 1.05,
            "freight": 101,
            "lme": float(lme),
            "note": "Extra +66 freight for High Carbon"
        }

    @staticmethod
    def formula_12(low: Decimal, high: Decimal) -> Dict:
        """
        Formula 12: Plastic (PMC)
        Same avg(low,high) × factor + premium pattern as the steel formulas, against a
        dedicated PMC bulletin symbol (see LMECalculator.get_symbols_for_product).

        TODO: factor/premium below are placeholders (1.0 / 0) pending the real business
        constants for Plastic pricing — confirm with the business before relying on this
        formula's output in production.
        """
        avg = FormulaEngine.calculate_average(low, high)
        factor = Decimal("1.0")
        premium = Decimal("0")
        lme = avg * factor + premium

        return {
            "formula_number": 12,
            "average": float(avg),
            "discount_multiplier": float(factor),
            "freight": float(premium),
            "lme": float(lme),
            "note": "PLACEHOLDER constants — confirm real PMC factor/premium with business"
        }


# Test the formula matching
if __name__ == "__main__":
    print("Testing Formula Matching...")
    print("=" * 70)

    test_cases = [
        ("HRP", "CHINA", "PRIME", 1),
        ("CRS", "CHINA", "SECONDARY", 2),
        ("CRS", "NETHERLANDS", "SECONDARY", 6),  # NETHERLANDS TEST
        ("CRS", "GERMANY", "SECONDARY", 6),
        ("HRS", "SPAIN", "SECONDARY", 7),
        ("HRP", "UAE", "PRIME", 1),
    ]

    for product, origin, quality, expected in test_cases:
        result = FormulaEngine.determine_formula(product, origin, quality)
        status = "✓" if result == expected else "✗"
        print(f"{status} {product:6} | {origin:15} | {quality:10} → Formula {result} (expected {expected})")

    print("=" * 70)
