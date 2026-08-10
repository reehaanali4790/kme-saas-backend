"""Unit tests for utils/parsing.py - pure functions, no fixtures/DB needed.

These pin down the exact behavior consolidated from ~8 duplicated endpoint-file
helpers, so a future edit here can't silently change what any of the modules
that migrate onto these functions depend on.
"""
from datetime import date
from decimal import Decimal

from utils.parsing import (
    extract_leading_number,
    parse_date,
    parse_decimal,
    parse_float,
    parse_formatted_number,
)


def test_parse_date_valid():
    assert parse_date("2026-07-20") == date(2026, 7, 20)


def test_parse_date_with_time_component():
    assert parse_date("2026-07-20T10:30:00") == date(2026, 7, 20)


def test_parse_date_invalid_or_empty():
    assert parse_date("not-a-date") is None
    assert parse_date("") is None
    assert parse_date(None) is None


def test_parse_float():
    assert parse_float("42.5") == 42.5
    assert parse_float(0) == 0.0
    assert parse_float(None) is None


def test_parse_decimal():
    assert parse_decimal("42.50") == Decimal("42.50")
    assert parse_decimal(None) is None
    assert parse_decimal("") is None
    assert parse_decimal("not-a-number") is None


def test_parse_decimal_does_not_strip_commas():
    """Distinguishes parse_decimal from parse_formatted_number - Decimal("12,345")
    is invalid, and that's intentional: DB-bound values should already be clean."""
    assert parse_decimal("12,345") is None


def test_parse_formatted_number_strips_commas_and_whitespace():
    assert parse_formatted_number("12,345.67") == 12345.67
    assert parse_formatted_number(" 1,000 ") == 1000.0
    assert parse_formatted_number("") is None
    assert parse_formatted_number(None) is None
    assert parse_formatted_number("garbage") is None


def test_extract_leading_number_from_messy_text():
    assert extract_leading_number("593.00 PER M/TON") == "593.00"
    assert extract_leading_number("97,252") == "97252"
    assert extract_leading_number("no digits here") is None
    assert extract_leading_number(None) is None


def test_extract_leading_number_passes_through_numeric_types():
    assert extract_leading_number(42) == 42
    assert extract_leading_number(42.5) == 42.5
