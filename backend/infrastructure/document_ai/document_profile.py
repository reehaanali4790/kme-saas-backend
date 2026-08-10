"""
Detect whether a PDF is text-based (selectable) or scan/image-only.
Used to route Gemini extraction: native PDF upload (text PDFs) vs page images (scans).
"""

from __future__ import annotations

from pathlib import Path


def pdf_extractable_text(file_path: str, max_pages: int | None = None) -> str:
    import fitz

    doc = fitz.open(file_path)
    parts: list[str] = []
    for i, page in enumerate(doc):
        if max_pages is not None and i >= max_pages:
            break
        parts.append(page.get_text())
    doc.close()
    return "\n".join(parts)


def is_text_pdf(file_path: str, min_chars: int = 200, max_pages: int | None = None) -> bool:
    ext = Path(file_path).suffix.lower()
    if ext != ".pdf":
        return False
    try:
        return len(pdf_extractable_text(file_path, max_pages=max_pages).strip()) >= min_chars
    except Exception:
        return False
