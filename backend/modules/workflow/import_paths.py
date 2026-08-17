"""Import path modes and path-aware required-documents matrix."""
from __future__ import annotations

from typing import Iterable, Optional

IMPORT_MODE_LC_BACKED = "LC_BACKED"
IMPORT_MODE_NON_LC = "NON_LC"
IMPORT_MODE_TT = "TT"
IMPORT_MODE_CAD = "CAD"

IMPORT_MODES = frozenset({
    IMPORT_MODE_LC_BACKED,
    IMPORT_MODE_NON_LC,
    IMPORT_MODE_TT,
    IMPORT_MODE_CAD,
})

NON_LC_MODES = frozenset({IMPORT_MODE_NON_LC, IMPORT_MODE_TT, IMPORT_MODE_CAD})

DOCS_RECEPTION_NOT_STARTED = "NOT_STARTED"
DOCS_RECEPTION_AWAITING = "AWAITING"
DOCS_RECEPTION_PARTIAL = "PARTIAL"
DOCS_RECEPTION_COMPLETE = "COMPLETE"

DOCS_RECEPTION_STATUSES = frozenset({
    DOCS_RECEPTION_NOT_STARTED,
    DOCS_RECEPTION_AWAITING,
    DOCS_RECEPTION_PARTIAL,
    DOCS_RECEPTION_COMPLETE,
})

DOC_BL = "bl"
DOC_INVOICE = "invoice"
DOC_PACKING = "packing"
DOC_FI = "fi"
DOC_INSURANCE = "insurance"
DOC_LC = "lc"
DOC_CONTRACT = "contract"

# Path-aware required core docs (before GD). Insurance remains advisory everywhere.
_REQUIRED_CORE: dict[str, frozenset[str]] = {
    IMPORT_MODE_LC_BACKED: frozenset({DOC_BL, DOC_INVOICE, DOC_PACKING, DOC_FI}),
    IMPORT_MODE_NON_LC: frozenset({DOC_BL, DOC_INVOICE, DOC_PACKING}),
    IMPORT_MODE_TT: frozenset({DOC_BL, DOC_INVOICE, DOC_PACKING}),
    IMPORT_MODE_CAD: frozenset({DOC_BL, DOC_INVOICE, DOC_PACKING}),
}


def normalize_import_mode(mode: Optional[str]) -> str:
    m = (mode or IMPORT_MODE_LC_BACKED).upper()
    return m if m in IMPORT_MODES else IMPORT_MODE_LC_BACKED


def is_lc_backed(mode: Optional[str]) -> bool:
    return normalize_import_mode(mode) == IMPORT_MODE_LC_BACKED


def fi_required(mode: Optional[str]) -> bool:
    return is_lc_backed(mode)


def required_core_docs(mode: Optional[str]) -> frozenset[str]:
    return _REQUIRED_CORE.get(normalize_import_mode(mode), _REQUIRED_CORE[IMPORT_MODE_LC_BACKED])


def is_doc_required(mode: Optional[str], doc_type: str) -> bool:
    dt = (doc_type or "").lower()
    if dt == DOC_INSURANCE:
        return False
    if dt == DOC_LC:
        return is_lc_backed(mode)
    if dt == DOC_CONTRACT:
        return True
    return dt in required_core_docs(mode)


def missing_required_docs(
    mode: Optional[str],
    *,
    has_bl: bool,
    has_invoice: bool,
    has_packing: bool,
    has_fi: bool,
) -> list[str]:
    present = {
        DOC_BL: has_bl,
        DOC_INVOICE: has_invoice,
        DOC_PACKING: has_packing,
        DOC_FI: has_fi,
    }
    labels = {
        DOC_BL: "BL",
        DOC_INVOICE: "Invoice",
        DOC_PACKING: "Packing",
        DOC_FI: "FI",
    }
    missing: list[str] = []
    for doc in required_core_docs(mode):
        if not present.get(doc):
            missing.append(labels.get(doc, doc.upper()))
    return missing


def import_mode_label(mode: Optional[str]) -> str:
    m = normalize_import_mode(mode)
    return {
        IMPORT_MODE_LC_BACKED: "LC-backed import",
        IMPORT_MODE_NON_LC: "Non-LC import",
        IMPORT_MODE_TT: "Non-LC (TT)",
        IMPORT_MODE_CAD: "Non-LC (CAD)",
    }.get(m, m)
