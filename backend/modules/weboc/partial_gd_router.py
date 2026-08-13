"""
Partial GD (EB Release) API — the business-facing "Partial GD" workflow.

Technically each Partial GD is an Ex-Bond Release linked to an existing Into-Bond GD
(ExBondEntry). Unlike the legacy single-document Ex-Bond GD flow in router.py (left
unchanged for backward compatibility), every Partial GD here carries its own EB GD View
and its own (potentially several) Item Detail documents, and the SRO number used for
quota validation is always extracted from THIS Partial GD's own item details.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from models.database_models import User
from modules.auth.dependencies import get_current_user
from core.permissions import require_min_role
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.weboc.extractors.gd_view_extractor_service import extract_gd_view
from modules.weboc.extractors.item_details_extractor_service import extract_item_details
from modules.weboc.gd_service import ALLOWED_EXTENSIONS
from modules.weboc.helpers.weboc_service import bond_summary
from . import services as svc
from . import partial_gd_service as pgd
from .schemas import PartialGdViewIn, PartialGdItemDetailsIn, PartialGdValidateApproval

logger = logging.getLogger("uvicorn")

partial_gd_router = APIRouter(prefix="/api/partial-gd", tags=["WeBOC — Partial GD (EB Release)"])


def _check_ext(file: UploadFile) -> tuple[str, str]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only JPG, PNG, PDF supported. Got: {ext}")
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not set up on this server. Please contact support, or enter the details manually.")
    enforce_document_quota()
    return ext, file.filename


@partial_gd_router.post("/{into_bond_gd_id}/start")
def start_partial_gd(
    into_bond_gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    entry = pgd.start_partial_gd(into_bond_gd_id, current_user.user_id, db)
    logger.info(f"Partial GD started: entry_id={entry.entry_id}, into_bond_gd_id={into_bond_gd_id}")
    return {"success": True, "entry_id": entry.entry_id}


@partial_gd_router.post("/{entry_id}/gd-view/upload-and-extract")
async def partial_gd_view_upload_and_extract(
    entry_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    ext, filename = _check_ext(file)
    entry = pgd.get_entry_or_error(entry_id, db)
    file_contents = await file.read()
    att = pgd.replace_entry_attachment(entry, "EX_BOND_GD_VIEW", filename, file_contents, db, current_user.user_id)
    db.commit()
    meter_document_accepted(file_path=att.file_path)

    extracted, extraction_error = safe_extract(
        extract_gd_view, att.file_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Partial GD EB GD View, entry_id={entry.entry_id}, file={filename}")

    if extraction_error:
        logger.warning(f"Partial GD {entry.entry_id}: EB GD View extraction failed — manual entry.")
        return {"entry_id": entry.entry_id, "attachment_id": att.attachment_id,
                "document_filename": att.filename, "is_pdf": ext == ".pdf",
                "extracted": {}, "warnings": [], "extraction_failed": True,
                "extraction_message": extraction_error}

    view_data = PartialGdViewIn(**extracted)
    pgd.apply_partial_gd_view(entry, view_data)
    db.commit()
    db.refresh(entry)

    warnings = []
    if svc.classify_declaration(extracted.get("declaration_type")) != "EX_BOND" and \
            svc.classify_from_gd_number(extracted.get("gd_number")) != "EX_BOND":
        warnings.append("This document does not look like an Ex-Bond (EB/XB) GD View — please verify.")

    # Early, non-blocking heads-up — the hard, unbypassable block happens at
    # validate-approval time (the actual "recording" of the release).
    dup = pgd.find_duplicate_gd_number(entry, db)
    if dup:
        warnings.append(
            f"EB GD Number '{entry.gd_number}' is already recorded on this Into-Bond GD "
            f"as Partial GD #{dup.entry_id} ({'finalized' if dup.is_finalized else 'draft'}). "
            f"Validating this one will be blocked as a duplicate."
        )

    return {"entry_id": entry.entry_id, "attachment_id": att.attachment_id,
            "document_filename": att.filename, "is_pdf": ext == ".pdf",
            "extracted": extracted, "warnings": warnings}


@partial_gd_router.post("/{entry_id}/item-details/upload-and-extract")
async def partial_gd_item_details_upload_and_extract(
    entry_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    ext, filename = _check_ext(file)
    entry = pgd.get_entry_or_error(entry_id, db)
    file_contents = await file.read()
    # Append — a Partial GD expects several Item Details documents, unlike the IB's own
    # (single, replace-semantics) Item Details.
    att = pgd.add_entry_attachment(entry, "EX_BOND_ITEM_DETAILS", filename, file_contents, db, current_user.user_id)
    db.commit()
    meter_document_accepted(file_path=att.file_path)

    extracted, extraction_error = safe_extract(
        extract_item_details, att.file_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Partial GD Item Details, entry_id={entry.entry_id}, file={filename}")

    if extraction_error:
        logger.warning(f"Partial GD {entry.entry_id}: Item Details extraction failed — manual entry.")
        return {"entry_id": entry.entry_id, "attachment_id": att.attachment_id,
                "document_filename": att.filename, "is_pdf": ext == ".pdf",
                "extracted": {"items": []}, "warnings": [], "extraction_failed": True,
                "extraction_message": extraction_error}

    parsed = PartialGdItemDetailsIn(**extracted)
    added = pgd.apply_partial_gd_item_details(entry, parsed.items or [], att.attachment_id, db)
    db.commit()

    warnings = []
    items = extracted.get("items") or []
    if items and not any(it.get("sro_no") or it.get("quota_reference") for it in items):
        warnings.append("No SRO / quota reference was found on any item in this document.")

    return {"entry_id": entry.entry_id, "attachment_id": att.attachment_id,
            "document_filename": att.filename, "is_pdf": ext == ".pdf",
            "extracted": extracted, "items_added": added, "warnings": warnings}


@partial_gd_router.get("/{entry_id}")
def get_partial_gd(
    entry_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    return pgd.get_partial_gd_detail(entry_id, db)


@partial_gd_router.post("/{entry_id}/validate-approval")
def validate_partial_gd_approval(
    entry_id: int,
    data: PartialGdValidateApproval,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    entry = pgd.validate_partial_gd_approval(entry_id, data, current_user.user_id, db)
    gd = svc.get_gd_or_error(entry.into_bond_gd_id, db)
    logger.info(f"Partial GD validated: entry_id={entry.entry_id}, approval_id={entry.approval_id}, "
                f"qty={entry.quantity_mt}")
    return {"success": True, "entry_id": entry.entry_id, "is_finalized": True,
            "bond": bond_summary(gd, db)}


@partial_gd_router.delete("/{entry_id}/item-details/{attachment_id}")
def delete_partial_gd_item_details(
    entry_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    entry = pgd.delete_partial_gd_item_details_doc(entry_id, attachment_id, db, deleted_by=current_user.user_id)
    gd = svc.get_gd_or_error(entry.into_bond_gd_id, db)
    logger.info(f"Partial GD Item Details document removed: entry_id={entry_id}, attachment_id={attachment_id}, "
                f"is_finalized={entry.is_finalized}")
    return {"success": True, "entry_id": entry_id, "attachment_id": attachment_id,
            "is_finalized": entry.is_finalized, "bond": bond_summary(gd, db)}


@partial_gd_router.delete("/{entry_id}")
def delete_partial_gd(
    entry_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = pgd.delete_partial_gd(entry_id, db, deleted_by=current_user.user_id)
    return {"success": True, "entry_id": entry_id, "bond": bond_summary(gd, db) if gd else None}


@partial_gd_router.get("/{entry_id}/document")
def partial_gd_document(
    entry_id: int,
    attachment_id: int = Query(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    att = pgd.get_entry_attachment_file(entry_id, attachment_id, db)
    return document_file_response(str(att.file_path), att.filename)
