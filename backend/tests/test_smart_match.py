"""Tests for smart fuzzy name matching."""
from infrastructure.normalization.smart_match import names_equivalent


def test_company_typo_limited():
    assert names_equivalent(
        "FORTUNE COMMODITY (HK) LIMTED",
        "FORTUNE COMMODITY (HK) LIMITED",
        kind="company",
    ) is True


def test_company_legal_form_variants():
    assert names_equivalent(
        "PERFECT CRAFT SMC PVT LTD.",
        "PERFECT CRAFT (SMC-PVT) LTD",
        kind="company",
    ) is True


def test_location_preposition_variants():
    assert names_equivalent(
        "ANY PORT OF CHINA",
        "ANY PORT IN CHINA",
        kind="location",
    ) is True


def test_bank_name_variants():
    assert names_equivalent(
        "ALBARAKA BANK (PAKISTAN) LIMITED",
        "AL BARAKA BANK PAKISTAN LTD",
        kind="bank",
    ) is True


def test_vessel_voyage_suffix():
    assert names_equivalent(
        "MV EFFIE V. V 12",
        "EFFIE V",
        kind="vessel",
    ) is True


def test_clearly_different_company():
    assert names_equivalent(
        "FORTUNE COMMODITY (HK) LIMITED",
        "RANGE INDUSTRIES LIMITED",
        kind="company",
    ) is False


def test_reference_substring():
    assert names_equivalent("SLC/0117/26/0468", "SLC0117260468", kind="reference") is True
