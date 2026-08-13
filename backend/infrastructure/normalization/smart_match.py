"""Intelligent fuzzy matching for cross-document name comparisons.

Used when comparing extracted values (LC, BL, invoice, contract, GD) so minor
OCR typos, legal-form variants, and preposition differences do not flood users
with false warnings.
"""
from __future__ import annotations

import difflib
import re
from typing import Literal, Optional

from infrastructure.normalization.normalization_service import (
    _LEGAL_TOKENS,
    company_key,
    norm_bank,
    norm_vessel,
)

MatchKind = Literal["company", "bank", "vessel", "location", "reference", "generic"]

# Common OCR / extraction typos in trade documents
_TYPO_MAP = {
    "LIMTED": "LIMITED",
    "LIMITE": "LIMITED",
    "LIMIED": "LIMITED",
    "LIMTIED": "LIMITED",
    "LTDED": "LIMITED",
    "LIMITEE": "LIMITED",
    "PVTD": "PVT",
    "PRVATE": "PRIVATE",
    "PRIVTE": "PRIVATE",
    "COMPANY": "CO",
    "CORPORATON": "CORPORATION",
    "CORPORATIOM": "CORPORATION",
    "CHIAN": "CHINA",
    "CHINa": "CHINA",
    "PAKISTAN": "PAKISTAN",
}

# Prepositions/articles ignored when comparing ports and locations
_LOC_STOPWORDS = frozenset({"OF", "IN", "AT", "FROM", "THE", "A", "AN", "OR", "AND"})

# Similarity thresholds per kind (SequenceMatcher ratio on normalized keys)
_CUTOFFS: dict[str, float] = {
    "company": 0.85,
    "bank": 0.88,
    "vessel": 0.88,
    "location": 0.90,
    "reference": 1.0,  # handled separately — substring/exact only
    "generic": 0.88,
}


def _fix_typos(text: str) -> str:
    s = str(text or "")
    for wrong, right in _TYPO_MAP.items():
        s = re.sub(rf"\b{re.escape(wrong)}\b", right, s, flags=re.IGNORECASE)
    return s


def _strip_legal_tokens(key: str) -> str:
    u = key
    for tok in _LEGAL_TOKENS:
        u = re.sub(rf"\b{re.escape(tok)}\b", " ", u)
    return re.sub(r"\s+", " ", u).strip()


def _company_match_key(name) -> str:
    return _strip_legal_tokens(company_key(_fix_typos(name)))


def _bank_match_key(name) -> str:
    canon = norm_bank(name)
    if not canon or canon == "(Unknown Bank)":
        return ""
    return re.sub(r"\s+", " ", canon.upper()).strip()


def _location_key(name) -> str:
    u = re.sub(r"[^A-Za-z0-9]+", " ", str(name or "")).strip().upper()
    u = re.sub(r"\s+", " ", u)
    tokens = [t for t in u.split() if t not in _LOC_STOPWORDS]
    return " ".join(tokens)


def _reference_key(name) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(name or "")).upper()


def _normalize(kind: MatchKind, value) -> str:
    if kind == "company":
        return _company_match_key(value)
    if kind == "bank":
        return _bank_match_key(value)
    if kind == "vessel":
        return norm_vessel(value) or ""
    if kind == "location":
        return _location_key(value)
    if kind == "reference":
        return _reference_key(value)
    # generic
    s = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "")).strip().upper()
    return re.sub(r"\s+", " ", s)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def names_equivalent(a, b, kind: MatchKind = "generic") -> Optional[bool]:
    """Return True if values are equivalent, False if clearly different, None if incomparable."""
    if not a or not b:
        return None

    ka, kb = _normalize(kind, a), _normalize(kind, b)
    if not ka or not kb:
        return None

    if kind == "reference":
        return ka == kb or ka in kb or kb in ka

    if ka == kb:
        return True
    if ka in kb or kb in ka:
        return True

    ratio = _similar(ka, kb)
    if ratio >= _CUTOFFS.get(kind, 0.88):
        return True

    # Token overlap — catches reordered words ("HK FORTUNE" vs "FORTUNE HK")
    ta, tb = set(ka.split()), set(kb.split())
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
        if overlap >= 0.85 and ratio >= 0.75:
            return True

    return False
