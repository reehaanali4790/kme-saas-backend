"""
LME Monitoring System - PDF Upload API (Production)
Upload and process FastMarkets PDF bulletins with currency rate integration
Version: 2.0
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
import tempfile
import os
import logging

from core.tenant import get_tenant_db
from core.permissions import require_permission
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.documents.extractors.pdf_price_extractor import extract_prices_from_pdf
from modules.documents import pdf_upload_service as svc
from modules.documents.pdf_upload_schemas import UploadBulletinPayload

router = APIRouter(prefix="/api/pdf", tags=["PDF Upload"])
logger = logging.getLogger("uvicorn")

MAX_PDF_SIZE = svc.MAX_PDF_SIZE

_can_upload = require_permission("upload_pdf")
_can_manage = require_permission("manage_users")


@router.post("/check-rates")
async def check_currency_rates(
    file: UploadFile = File(...),
    current_user: User = Depends(_can_upload),
    db: Session = Depends(get_tenant_db)
):
    """
    Check if currency rates exist for the PDF's bulletin date
    Returns rate info or suggests what to do
    """
    if not file.filename or not svc.is_valid_pdf(file.filename):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    contents = await file.read()

    if len(contents) > MAX_PDF_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds limit (10MB)")

    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        extraction_result = extract_prices_from_pdf(tmp_path)

        if not extraction_result.get('bulletin_date'):
            raise HTTPException(status_code=400, detail="Failed to extract bulletin date")

        result = svc.rate_availability(db, extraction_result['bulletin_date'])
        # Hand the already-parsed prices back so the frontend can pass them straight
        # to /upload-bulletin instead of re-uploading the file for a second PDF parse.
        result["extraction"] = {
            "prices": extraction_result["prices"],
            "symbols_found": extraction_result["symbols_found"],
        }
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/upload-bulletin")
def upload_bulletin(
    payload: UploadBulletinPayload,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(_can_upload),
    db: Session = Depends(get_tenant_db)
):
    """
    Store a bulletin from prices already extracted by /check-rates (no re-parsing).
    Requires currency rates (existing rate_id OR new usd_rate+eur_rate).
    """
    if not payload.prices:
        raise HTTPException(status_code=400, detail="Failed to extract prices from PDF")

    bulletin_date = payload.bulletin_date
    symbols_found = len(payload.symbols_found)
    prices_data = [p.model_dump() for p in payload.prices]

    try:
        currency_rate = svc.resolve_currency_rate(
            db, bulletin_date, payload.rate_id, payload.usd_rate, payload.eur_rate,
            int(current_user.user_id), current_user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        bulletin, prices_stored = svc.replace_bulletin_prices(
            db, bulletin_date, payload.file_name, int(current_user.user_id),
            symbols_found, prices_data, currency_rate)

        # Generate price change alerts for LCs opened in last 40 days (non-critical)
        alert_summary = svc.generate_bulletin_alerts(db, bulletin.bulletin_id)

        # Push the new alerts out over WhatsApp (non-critical — upload still
        # succeeds even if delivery fails). No-op unless WhatsApp is enabled.
        svc.dispatch_whatsapp_alerts(db, alert_summary["alerts_created"])

        # Also send the branded LME rates report for the bulletin just loaded —
        # same treatment whether a bulletin arrives by manual upload or crawler.
        svc.dispatch_whatsapp_rates_report(db)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing bulletin: {str(e)}")
        logger.exception("Full traceback:")
        raise HTTPException(status_code=500, detail=str(e))

    # Baseline auto-fill + current_lme recalc each loop over every pending/active LC —
    # run that after the response is sent instead of making the upload wait on it.
    background_tasks.add_task(svc.recalculate_lcs_for_bulletin, bulletin_date, bulletin.bulletin_id)

    return {
        "success": True,
        "message": f"Bulletin processed successfully. {symbols_found} prices extracted. "
                   "LC baseline/LME reconciliation is running in the background.",
        "data": {
            "bulletin_id": bulletin.bulletin_id,
            "bulletin_date": str(bulletin_date),
            "symbols_found": symbols_found,
            "prices_stored": prices_stored,
            "alerts_generated": alert_summary["alerts_created"],
            "currency_rate": {
                "usd_rate": float(currency_rate.usd_rate),
                "eur_rate": float(currency_rate.eur_rate)
            }
        }
    }


@router.get("/bulletins/list")
def list_bulletins(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_tenant_db)
):
    """List all uploaded bulletins"""
    try:
        return svc.bulletins_list(db, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bulletins/{bulletin_id}")
def delete_bulletin(
    bulletin_id: int,
    current_user: User = Depends(_can_manage),
    db: Session = Depends(get_tenant_db)
):
    """Delete a bulletin and its prices (Admin only)"""
    svc.delete_bulletin(db, bulletin_id)
    return {"success": True, "message": "Bulletin deleted successfully"}
