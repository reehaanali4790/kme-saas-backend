"""Shared request-body coercion helpers.

These consolidate parsers that were copy-pasted across ~8 endpoint files with
real behavioral differences (float vs Decimal, comma-stripping vs not, regex
extraction for messy AI-extracted text) - each function here is named for what
it actually does, rather than reusing the original files' ambiguous `_num`/`_dec`
names, which meant different things in different files.

Existing modules keep their own copies until they're individually migrated (see
the reference-migration pattern) - swapping all ~8 files over in one shot risks
a subtle behavior difference slipping through unnoticed; migrating file-by-file
with tests is safer.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Union


def parse_date(v) -> Optional[date]:
    """'2026-07-20' or '2026-07-20T10:00:00' (or a date/datetime) -> date, else None."""
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def parse_float(v) -> Optional[float]:
    """Straight float-or-None, for values that are already numeric-ish (e.g.
    computed report figures) - not for raw user/AI input, which may have
    comma-thousands formatting (see parse_formatted_number)."""
    return float(v) if v is not None else None


def parse_decimal(v) -> Optional[Decimal]:
    """Strict Decimal-or-None, for DB Numeric columns where float rounding error
    is unacceptable. Does not strip formatting - pass already-clean values."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_formatted_number(v) -> Optional[float]:
    """Float-or-None, tolerant of comma-thousands formatting ('12,345.67') and
    surrounding whitespace - for user-entered amounts."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def extract_leading_number(v) -> Optional[Union[str, int, float]]:
    """Pull the leading number out of messy AI-extracted text, e.g.
    '593.00 PER M/TON' -> '593.00', '97,252' -> '97252'. Returns a numeric
    string so callers can feed it into whichever type they need (int/float
    values pass through unchanged rather than being stringified).
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    match = re.search(r"[-+]?\d[\d,]*\.?\d*", str(v))
    return match.group().replace(",", "") if match else None
