"""
Branding config endpoints — app name, logo image, logo background color.

GET endpoints are deliberately public (no auth dependency): the login screen and
the app shell render brand info before a session exists, and a plain <img src=...>
tag can't attach an Authorization header anyway. Writes are admin-only, reusing the
same "manage_users" gate the WhatsApp admin config uses (modules/alerts/router.py).
"""
import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db, get_default_tenant_db
from config.settings import settings
from core.permissions import require_permission
from infrastructure.documents.document_files import document_file_response
from models.database_models import User
from modules.branding import service as svc
from modules.branding.schemas import BrandingConfigOut, BrandingConfigUpdate
from utils.uploads import safe_upload_path

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/branding", tags=["Branding"])

_require_admin = require_permission("manage_users")

BRANDING_UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "branding")
ALLOWED_LOGO_EXTENSIONS = {".png", ".svg", ".webp"}
MAX_LOGO_SIZE = 2 * 1024 * 1024  # 2 MB


def _safe_remove(path) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.get("", response_model=BrandingConfigOut)
def get_branding(db: Session = Depends(get_default_tenant_db)):
    """Public — current app name / logo / background color (no org session required)."""
    return svc.to_out(svc.get_or_create_config(db))


@router.put("", response_model=BrandingConfigOut)
def update_branding(
    data: BrandingConfigUpdate,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    cfg = svc.update_config(data, db, updated_by=current_user.user_id)
    logger.info(f"Branding config updated by {current_user.username}")
    return svc.to_out(cfg)


@router.post("/logo", response_model=BrandingConfigOut)
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    """Upload a new logo. Transparent-background PNG/SVG/WEBP recommended — the logo
    is displayed forced to solid white via CSS filter, so a non-transparent background
    on the image would just render as a solid-color box."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext or 'unknown'}' not allowed. Use PNG, SVG or WEBP.",
        )

    contents = await file.read()
    if len(contents) > MAX_LOGO_SIZE:
        raise HTTPException(status_code=413, detail="Logo must be 2 MB or smaller.")

    os.makedirs(BRANDING_UPLOAD_DIR, exist_ok=True)
    dest = safe_upload_path(BRANDING_UPLOAD_DIR, "logo", file.filename, ALLOWED_LOGO_EXTENSIONS)
    with open(dest, "wb") as f:
        f.write(contents)

    cfg = svc.get_or_create_config(db)
    old_path = cfg.logo_path
    cfg.logo_path = dest
    cfg.logo_original_filename = file.filename
    cfg.updated_by = current_user.user_id
    db.commit()
    db.refresh(cfg)

    if old_path and old_path != dest:
        _safe_remove(old_path)

    logger.info(f"Branding logo uploaded by {current_user.username}: {file.filename}")
    return svc.to_out(cfg)


@router.delete("/logo", response_model=BrandingConfigOut)
def reset_logo(
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(_require_admin),
):
    cfg = svc.get_or_create_config(db)
    _safe_remove(cfg.logo_path)
    cfg.logo_path = None
    cfg.logo_original_filename = None
    cfg.updated_by = current_user.user_id
    db.commit()
    db.refresh(cfg)
    return svc.to_out(cfg)


@router.get("/logo")
def get_logo(db: Session = Depends(get_default_tenant_db)):
    """Public — serves the raw logo image bytes for <img src>."""
    cfg = svc.get_or_create_config(db)
    if not cfg.logo_path:
        raise HTTPException(status_code=404, detail="No logo configured")
    return document_file_response(cfg.logo_path, cfg.logo_original_filename)
