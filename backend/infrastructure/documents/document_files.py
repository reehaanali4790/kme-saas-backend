"""Serve uploaded documents with correct Content-Type for in-browser viewing.

Ported from rehan/newchanges (backend/services/document_files.py).
"""

from __future__ import annotations

import mimetypes
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse
from config.settings import settings

_EXT_MEDIA = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def media_type_for(filename: str | None, path: str | None = None) -> str:
    name = filename or (os.path.basename(path) if path else "")
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    ext = os.path.splitext(name)[1].lower()
    return _EXT_MEDIA.get(ext, "application/octet-stream")


def resolve_document_path(path: str | None) -> str | None:
    """Find document file across standard upload directories even if working dir or volume path moved."""
    if not path:
        return None
    if os.path.exists(path):
        return path

    filename = os.path.basename(path)
    if not filename:
        return None

    subfolders = [
        "invoice_documents",
        "bl_documents",
        "packing_documents",
        "gd_documents",
        "fi_documents",
        "insurance_documents",
        "branding",
        "pdfs",
        "excel",
        "",
    ]

    base_dirs = [
        settings.UPLOAD_DIR,
        "/data/uploads",
        "/data",
        "./uploads",
        "uploads",
    ]

    for b in base_dirs:
        for sub in subfolders:
            candidate = os.path.join(b, sub, filename) if sub else os.path.join(b, filename)
            if os.path.exists(candidate):
                return candidate

    return None


def document_file_response(path: str, filename: str | None = None) -> FileResponse:
    """Return the original uploaded file with an explicit media type."""
    real_path = resolve_document_path(path)
    if not real_path:
        raise HTTPException(
            status_code=404,
            detail="Document file not found on server storage. Please use Re-upload to attach the document.",
        )
    name = filename or os.path.basename(real_path)
    return FileResponse(
        real_path,
        filename=name,
        media_type=media_type_for(name, real_path),
    )
