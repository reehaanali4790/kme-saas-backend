"""Serve uploaded documents with correct Content-Type for in-browser viewing.

Ported from rehan/newchanges (backend/services/document_files.py).
"""

from __future__ import annotations

import mimetypes
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse
from config.settings import settings
from core.tenant_upload import get_current_tenant_schema

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


def _allowed_roots() -> list[str]:
    schema = get_current_tenant_schema()
    roots = []
    if schema:
        roots.append(os.path.abspath(os.path.join(settings.UPLOAD_DIR, schema)))
    else:
        roots.append(os.path.abspath(settings.UPLOAD_DIR))
    return roots


def _is_under_allowed_root(path: str) -> bool:
    abs_path = os.path.abspath(path)
    for root in _allowed_roots():
        try:
            if os.path.commonpath([abs_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def resolve_document_path(path: str | None) -> str | None:
    """Resolve a stored path inside the current tenant's upload directory only."""
    if not path:
        return None
    if os.path.exists(path) and _is_under_allowed_root(path):
        return path

    filename = os.path.basename(path)
    if not filename:
        return None

    subfolders = [
        "invoice_documents",
        "bl_documents",
        "packing_documents",
        "gd_documents",
        "gd_attachments",
        "fi_documents",
        "insurance_documents",
        "lc_documents",
        "contract_documents",
        "shipment_documents",
        "branding",
        "pdfs",
        "excel",
        "",
    ]

    for root in _allowed_roots():
        for sub in subfolders:
            candidate = os.path.join(root, sub, filename) if sub else os.path.join(root, filename)
            if os.path.exists(candidate) and _is_under_allowed_root(candidate):
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
