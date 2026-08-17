"""Helpers for surfacing combined-PDF segmentation info in upload API responses."""
from __future__ import annotations

from typing import Any

DOC_LABELS = {
    "bl": "Bill of Lading",
    "invoice": "Commercial Invoice",
    "packing": "Packing List",
    "fi": "Financial Instrument",
    "insurance": "Insurance",
}


def segmentation_warnings(extracted: dict[str, Any], doc_type: str) -> list[str]:
    """Build user-facing warnings when a combined PDF was segmented before extraction."""
    seg = extracted.get("_segmentation") if isinstance(extracted, dict) else None
    if not seg or not isinstance(seg, dict):
        return []

    warnings: list[str] = []
    if seg.get("is_combined"):
        label = DOC_LABELS.get(doc_type, doc_type)
        pages = seg.get("pages_used") or []
        page_str = ", ".join(str(p) for p in pages) if pages else "selected pages"
        method = seg.get("method", "auto")
        warnings.append(
            f"This PDF contains multiple documents. The {label} was extracted from "
            f"page(s) {page_str} ({method} detection)."
        )
        other = [
            f"{s.get('document_type')} (pp. {s.get('page_start')}-{s.get('page_end')})"
            for s in (seg.get("segments") or [])
            if s.get("document_type") != seg.get("target_document_type")
        ]
        if other:
            warnings.append(
                "Other sections detected in this file: " + "; ".join(other) + ". "
                "Upload each document type separately if you need to save them all."
            )
    return warnings


def strip_extraction_internals(extracted: dict[str, Any]) -> dict[str, Any]:
    """Remove pipeline metadata before returning extracted fields to the client."""
    if not isinstance(extracted, dict):
        return extracted
    for key in ("_segmentation", "_extraction_method", "_extraction_partial"):
        extracted.pop(key, None)
    return extracted
