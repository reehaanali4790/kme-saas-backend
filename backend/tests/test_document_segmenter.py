"""Unit tests for combined-PDF page segmentation."""
from infrastructure.document_ai.document_segmenter import (
    _pages_to_segments,
    _score_page_text,
    _pages_for_canonical,
)
from infrastructure.document_ai.segmentation_response import segmentation_warnings, strip_extraction_internals


def test_score_page_text_prefers_invoice_heading():
    text = "COMMERCIAL INVOICE\nInvoice No: INV-123\nUnit Price USD/MT"
    assert _score_page_text(text, "COMMERCIAL_INVOICE") > _score_page_text(text, "BILL_OF_LADING")


def test_pages_to_segments_merges_consecutive_same_type():
    pages = [
        {"page": 1, "document_type": "COMMERCIAL_INVOICE", "confidence": 0.9},
        {"page": 2, "document_type": "COMMERCIAL_INVOICE", "confidence": 0.9},
        {"page": 3, "document_type": "PACKING_LIST", "confidence": 0.9},
        {"page": 4, "document_type": "BILL_OF_LADING", "confidence": 0.9},
    ]
    segments = _pages_to_segments(pages)
    assert segments == [
        {"document_type": "COMMERCIAL_INVOICE", "page_start": 1, "page_end": 2},
        {"document_type": "PACKING_LIST", "page_start": 3, "page_end": 3},
        {"document_type": "BILL_OF_LADING", "page_start": 4, "page_end": 4},
    ]


def test_pages_for_canonical_collects_page_numbers():
    segments = [
        {"document_type": "PACKING_LIST", "page_start": 3, "page_end": 4},
        {"document_type": "BILL_OF_LADING", "page_start": 5, "page_end": 6},
    ]
    assert _pages_for_canonical(segments, "PACKING_LIST") == [3, 4]


def test_segmentation_warnings_for_combined_pdf():
    extracted = {
        "invoice_number": "INV-1",
        "_segmentation": {
            "is_combined": True,
            "method": "text",
            "target_document_type": "COMMERCIAL_INVOICE",
            "pages_used": [1, 2],
            "segments": [
                {"document_type": "COMMERCIAL_INVOICE", "page_start": 1, "page_end": 2},
                {"document_type": "PACKING_LIST", "page_start": 3, "page_end": 3},
            ],
        },
    }
    warnings = segmentation_warnings(extracted, "invoice")
    assert any("multiple documents" in w.lower() for w in warnings)
    strip_extraction_internals(extracted)
    assert "_segmentation" not in extracted
