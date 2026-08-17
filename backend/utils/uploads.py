"""Shared helper for turning a client-supplied upload filename into a safe
on-disk path.

Every upload module used to build its destination path as
os.path.join(UPLOAD_DIR, f"{record_id}_{upload.filename}") - the raw
client-controlled filename went straight into the path with no basename()/
character sanitization and no containment check, so a crafted filename
(e.g. containing "/", "\\" or ".." segments) could escape UPLOAD_DIR.
This centralizes the fix in one place instead of patching each module.
"""
import os
import re

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STUB_LEN = 80


def safe_upload_path(upload_dir: str, record_id, client_filename: str | None, allowed_extensions: set) -> str:
    """Build "<upload_dir>/<record_id>_<sanitized-stub><ext>", guaranteed to
    resolve inside upload_dir. Raises ValueError on a disallowed extension or
    if the resulting path would somehow still escape upload_dir.
    """
    basename = os.path.basename(client_filename or "")
    stub, ext = os.path.splitext(basename)
    ext = ext.lower()
    if ext not in allowed_extensions:
        raise ValueError(f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(allowed_extensions))}")

    safe_stub = _SAFE_CHARS_RE.sub("_", stub).strip("._")[:_MAX_STUB_LEN] or "file"
    dest = os.path.join(upload_dir, f"{record_id}_{safe_stub}{ext}")

    upload_dir_abs = os.path.abspath(upload_dir)
    dest_abs = os.path.abspath(dest)
    if os.path.commonpath([dest_abs, upload_dir_abs]) != upload_dir_abs:
        raise ValueError("Resulting upload path escapes the upload directory")

    return dest


def tenant_upload_dir(base_upload_dir: str, tenant_schema: str, subdir: str) -> str:
    """Tenant-isolated upload directory: uploads/{tenant_schema}/{subdir}/"""
    path = os.path.join(base_upload_dir, tenant_schema, subdir)
    os.makedirs(path, exist_ok=True)
    return path


def tenant_doc_dir(tenant_schema: str, subdir: str) -> str:
    """Resolve tenant-scoped permanent upload dir using app settings."""
    from config.settings import settings
    return tenant_upload_dir(settings.UPLOAD_DIR, tenant_schema, subdir)
