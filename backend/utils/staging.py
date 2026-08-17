"""Shared file-staging helpers for upload-and-extract flows.

Extract endpoints stage files under ``<upload_dir>/_staged/`` and return JSON only.
Save endpoints promote staged files into permanent per-entity paths on explicit save.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Optional, Tuple

from fastapi import UploadFile

from config.settings import settings
from utils.uploads import safe_upload_path


def upload_dir(subdir: str) -> str:
    from core.tenant_upload import get_current_tenant_schema
    from utils.uploads import tenant_doc_dir

    schema = get_current_tenant_schema()
    if schema:
        return tenant_doc_dir(schema, subdir)
    return os.path.join(settings.UPLOAD_DIR, subdir)


def staged_dir(subdir: str, perm_dir: Optional[str] = None) -> str:
    root = perm_dir or upload_dir(subdir)
    return os.path.join(root, "_staged")


def stage_bytes(
    contents: bytes,
    subdir: str,
    allowed_extensions: set[str],
    original_filename: str,
    perm_dir: Optional[str] = None,
) -> Tuple[str, str]:
    """Write raw bytes to a temp staged path. Returns (staged_name, stage_path)."""
    ext = Path(original_filename).suffix.lower()
    if ext not in allowed_extensions:
        raise ValueError(f"Unsupported extension: {ext}")

    stage_root = staged_dir(subdir, perm_dir)
    os.makedirs(stage_root, exist_ok=True)
    staged_name = f"{uuid.uuid4().hex}{ext}"
    stage_path = os.path.join(stage_root, staged_name)
    with open(stage_path, "wb") as out:
        out.write(contents)
    return staged_name, stage_path


def stage_upload(
    file: UploadFile,
    subdir: str,
    allowed_extensions: set[str],
    perm_dir: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Write *file* to a temp staged path. Returns (staged_name, stage_path, ext)."""
    if not file.filename:
        raise ValueError("No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_extensions:
        raise ValueError(f"Unsupported extension: {ext}")

    stage_root = staged_dir(subdir, perm_dir)
    os.makedirs(stage_root, exist_ok=True)
    staged_name = f"{uuid.uuid4().hex}{ext}"
    stage_path = os.path.join(stage_root, staged_name)
    with open(stage_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return staged_name, stage_path, ext


def promote_staged(
    subdir: str,
    staged_file: str,
    entity_id: int,
    original_filename: Optional[str],
    allowed_extensions: set[str],
    perm_dir: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Move a staged file into the permanent upload dir. Returns (path, filename)."""
    if not staged_file:
        return None, None

    perm_dir = perm_dir or upload_dir(subdir)
    os.makedirs(perm_dir, exist_ok=True)
    stage_path = os.path.join(staged_dir(subdir, perm_dir), os.path.basename(staged_file))
    if not os.path.exists(stage_path):
        return None, None

    orig = original_filename or f"document{Path(stage_path).suffix}"
    dest = safe_upload_path(perm_dir, entity_id, orig, allowed_extensions)
    os.replace(stage_path, dest)
    return dest, orig


def remove_staged(subdir: str, staged_file: str, perm_dir: Optional[str] = None) -> None:
    if not staged_file:
        return
    path = os.path.join(staged_dir(subdir, perm_dir), os.path.basename(staged_file))
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def replace_document_path(record, dest: str, orig: str, *, source: str = "UPLOADED") -> None:
    """Attach promoted file to a document model, removing the prior file if replaced."""
    old_path = getattr(record, "document_path", None)
    record.document_path = dest
    record.document_filename = orig
    if hasattr(record, "source"):
        record.source = source
    if old_path and old_path != dest and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
