"""
Detect which pages of a combined PDF belong to each shipment document type.

Trade shipments often arrive as one PDF containing Commercial Invoice + Packing List +
Bill of Lading (sometimes FI / insurance). This module scores each page (text heuristics
first, lightweight AI vision when the PDF is a scan) and returns the page numbers to send
to the type-specific extractor.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn")

TARGET_TYPES = frozenset({"bl", "invoice", "packing", "fi", "insurance"})

CANONICAL = {
    "bl": "BILL_OF_LADING",
    "invoice": "COMMERCIAL_INVOICE",
    "packing": "PACKING_LIST",
    "fi": "FINANCIAL_INSTRUMENT",
    "insurance": "INSURANCE",
}

PAGE_SIGNALS: dict[str, list[str]] = {
    "BILL_OF_LADING": [
        "bill of lading", "b/l no", "b/l number", "bl no", "ocean vessel", "voy.no",
        "consignee", "notify party", "notify address", "port of loading", "port of discharge",
        "shipped on board", "freight prepaid", "freight collect", "place of receipt",
        "number of original b", "said to contain", "container no", "marks & nos",
    ],
    "COMMERCIAL_INVOICE": [
        "commercial invoice", "invoice no", "invoice number", "seller", "buyer", "consignee",
        "unit price", "usd/mt", "total amount", "total value", "incoterms", "documentary credit",
        "letter of credit", "l/c no", "amount in words", "for and on behalf of",
    ],
    "PACKING_LIST": [
        "packing list", "packing slip", "p/l no", "packing no", "packing date",
        "number of coils", "net weight", "gross weight", "total coils",
        "total net weight", "total gross weight",
    ],
    "FINANCIAL_INSTRUMENT": [
        "financial instrument", "psw portal", "view import", "mode of payment",
        "financial instrument unique", "trader ntn", "trader iban", "lc/contract no",
        "exchange rate", "financial instrument value",
    ],
    "INSURANCE": [
        "insurance policy", "policy no", "policy number", "insured", "insurer",
        "sum insured", "marine cargo", "certificate of insurance", "cover note",
    ],
}

CROSS_PENALTIES: dict[str, list[str]] = {
    "BILL_OF_LADING": ["commercial invoice", "packing list", "packing slip", "goods declaration"],
    "COMMERCIAL_INVOICE": ["bill of lading", "packing list", "packing slip"],
    "PACKING_LIST": ["bill of lading", "commercial invoice"],
}

SEGMENTATION_PROMPT = """You are classifying pages of a trade-document PDF. A single file may
contain several documents stapled together, commonly:
  - Commercial Invoice (CI)
  - Packing List (PL)
  - Bill of Lading (B/L)
  - Financial Instrument (FI / LC printout)
  - Insurance certificate

You will receive one image per page, in order (page 1 first). For EACH page, identify the
primary document type on that page.

Return ONLY valid JSON:
{
  "pages": [
    {"page": 1, "document_type": "COMMERCIAL_INVOICE", "confidence": 0.95},
    {"page": 2, "document_type": "COMMERCIAL_INVOICE", "confidence": 0.90}
  ],
  "segments": [
    {"document_type": "COMMERCIAL_INVOICE", "page_start": 1, "page_end": 2},
    {"document_type": "PACKING_LIST", "page_start": 3, "page_end": 4},
    {"document_type": "BILL_OF_LADING", "page_start": 5, "page_end": 6}
  ]
}

document_type must be one of:
  BILL_OF_LADING, COMMERCIAL_INVOICE, PACKING_LIST, FINANCIAL_INSTRUMENT, INSURANCE, OTHER

Rules:
- Use the page heading/title as the primary signal ("COMMERCIAL INVOICE", "PACKING LIST", etc.).
- Continuation pages of the same document (line-item tables, rider pages) keep the same type.
- A B/L rider / attachment listing coils/containers is still BILL_OF_LADING.
- If a page is blank or unreadable, use OTHER with low confidence.
- segments must cover every classified page in order with no overlaps.
"""


def pdf_page_count(file_path: str) -> int:
    import fitz

    doc = fitz.open(file_path)
    n = doc.page_count
    doc.close()
    return n


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _score_page_text(text: str, canonical_type: str) -> float:
    norm = _normalize_text(text)
    if not norm:
        return 0.0
    score = 0.0
    for sig in PAGE_SIGNALS.get(canonical_type, []):
        if sig in norm:
            score += 1.0
            if norm.find(sig) < 120:
                score += 0.5
    for bad in CROSS_PENALTIES.get(canonical_type, []):
        if bad in norm:
            score -= 0.75
    return score


def segment_by_text(file_path: str) -> dict[str, Any]:
    """Classify each page using extractable PDF text (fast, no AI cost)."""
    import fitz

    doc = fitz.open(file_path)
    page_rows: list[dict[str, Any]] = []
    for i, page in enumerate(doc):
        page_num = i + 1
        text = page.get_text()
        scores = {t: _score_page_text(text, t) for t in PAGE_SIGNALS}
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        page_rows.append({
            "page": page_num,
            "document_type": best_type if best_score >= 1.0 else "OTHER",
            "confidence": min(0.99, best_score / 4.0) if best_score >= 1.0 else 0.2,
        })
    doc.close()

    segments = _pages_to_segments(page_rows)
    types_found = {p["document_type"] for p in page_rows if p["document_type"] != "OTHER"}
    return {
        "method": "text",
        "pages": page_rows,
        "segments": segments,
        "is_combined": len(types_found) > 1,
    }


def _pages_to_segments(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not pages:
        return []
    segments: list[dict[str, Any]] = []
    cur_type = pages[0]["document_type"]
    start = pages[0]["page"]
    prev = pages[0]["page"]
    for p in pages[1:]:
        if p["document_type"] == cur_type and p["page"] == prev + 1:
            prev = p["page"]
            continue
        segments.append({"document_type": cur_type, "page_start": start, "page_end": prev})
        cur_type = p["document_type"]
        start = p["page"]
        prev = p["page"]
    segments.append({"document_type": cur_type, "page_start": start, "page_end": prev})
    return [s for s in segments if s["document_type"] != "OTHER"]


def segment_by_ai(file_path: str, api_key: str) -> dict[str, Any]:
    """Vision classification for scanned / low-text PDFs."""
    import httpx
    from config.settings import settings
    from infrastructure.document_ai.document_ai import (
        GEMINI_DEFAULT_MODEL,
        _gemini_parts,
        parse_json_response,
    )

    gkey = settings.GEMINI_API_KEY or api_key
    if not gkey:
        raise ValueError("No Gemini or Anthropic API key for page segmentation")

    parts = _gemini_parts(
        file_path, SEGMENTATION_PROMPT, force_images=True, zoom=1.0,
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_DEFAULT_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    with httpx.Client(timeout=90.0) as client:
        resp = client.post(url, params={"key": gkey}, json=payload)
        resp.raise_for_status()
        body = resp.json()
    candidates = body.get("candidates") or []
    parts_out = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts_out).strip()
    data, _partial = parse_json_response(raw)

    pages = data.get("pages") or []
    segments = data.get("segments") or _pages_to_segments(pages)
    types_found = {s["document_type"] for s in segments if s.get("document_type") != "OTHER"}
    return {
        "method": "ai",
        "pages": pages,
        "segments": segments,
        "is_combined": len(types_found) > 1,
    }


def _pages_for_canonical(segments: list[dict], canonical: str) -> list[int]:
    pages: list[int] = []
    for seg in segments:
        if seg.get("document_type") != canonical:
            continue
        start = int(seg["page_start"])
        end = int(seg["page_end"])
        pages.extend(range(start, end + 1))
    return sorted(set(pages))


def resolve_page_indices(
    file_path: str,
    doc_type: str,
    api_key: str,
) -> tuple[list[int] | None, dict[str, Any]]:
    """Return 1-based page numbers to extract for *doc_type*, or None to use all pages."""
    doc_type = (doc_type or "").lower()
    if doc_type not in TARGET_TYPES:
        return None, {}
    if Path(file_path).suffix.lower() != ".pdf":
        return None, {}

    total = pdf_page_count(file_path)
    if total <= 1:
        return None, {"total_pages": total, "is_combined": False}

    canonical = CANONICAL[doc_type]
    meta: dict[str, Any] = {"total_pages": total}

    text_seg = segment_by_text(file_path)
    target_pages = _pages_for_canonical(text_seg.get("segments") or [], canonical)
    is_combined = bool(text_seg.get("is_combined"))

    from infrastructure.document_ai.document_profile import is_text_pdf

    text_rich = is_text_pdf(file_path, min_chars=150)

    if target_pages and is_combined:
        meta.update({
            "method": "text",
            "is_combined": True,
            "target_document_type": canonical,
            "pages_used": target_pages,
            "segments": text_seg.get("segments"),
        })
        logger.info(
            f"Combined PDF ({Path(file_path).name}): {doc_type} -> pages {target_pages} "
            f"(text segmentation, {total} pages total)"
        )
        return target_pages, meta

    if (not text_rich or not target_pages) and api_key:
        try:
            ai_seg = segment_by_ai(file_path, api_key)
            ai_pages = _pages_for_canonical(ai_seg.get("segments") or [], canonical)
            if ai_pages and ai_seg.get("is_combined"):
                meta.update({
                    "method": "ai",
                    "is_combined": bool(ai_seg.get("is_combined")),
                    "target_document_type": canonical,
                    "pages_used": ai_pages,
                    "segments": ai_seg.get("segments"),
                })
                logger.info(
                    f"Combined PDF ({Path(file_path).name}): {doc_type} -> pages {ai_pages} "
                    f"(AI segmentation, {total} pages total)"
                )
                return ai_pages, meta
            meta["ai_segmentation"] = ai_seg
        except Exception as e:
            logger.warning(f"AI segmentation failed for {file_path}: {e}")
            meta["segmentation_error"] = str(e)

    meta["is_combined"] = is_combined
    meta["pages_used"] = list(range(1, total + 1))
    return None, meta
