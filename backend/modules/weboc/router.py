"""
WeBOC GD tab API — document upload flows for HC and Into-Bond GD types.
"""

import os
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from infrastructure.documents.document_files import document_file_response
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from config.settings import settings
from models.database_models import User, GoodsDeclaration
from modules.auth.dependencies import get_current_user
from modules.weboc.gd_service import (
    recompute_gd_status, gd_is_closed, ALLOWED_EXTENSIONS
)
from core.permissions import require_min_role
from core.exceptions import NotFoundError
from infrastructure.document_ai.document_ai import safe_extract
from core.platform_metering import enforce_document_quota, meter_document_accepted
from modules.weboc.extractors.gd_view_extractor_service import extract_gd_view
from modules.weboc.extractors.item_details_extractor_service import extract_item_details
from modules.weboc.extractors.gd_extractor_service import extract_gd
from modules.weboc.helpers.kgtl_service import kgtl_summary, quantity_reconciliation
from modules.weboc.helpers.weboc_service import (
    filing_deadline, bond_summary,
    cross_check_gd_view, cross_check_item_details
)
from modules.weboc.helpers.sro_usage import gd_sro_usage
from . import services as svc
from .schemas import (
    GDViewSave, ItemDetailsSave, IntoBondGDSave, ExBondGDSave, ExBondEntrySave,
    GdKgtlWeighmentSave, BondPenaltySave, DeclarationTypeOverride
)
from modules.workflow.constants import (
    ACTION_UPLOAD_GD_VIEW, ACTION_UPLOAD_ITEM_DETAILS,
    ACTION_UPLOAD_INTO_BOND_GD, ACTION_UPLOAD_EX_BOND_GD,
)
from modules.workflow.helpers import check_gate

logger = logging.getLogger("uvicorn")

gd_view_router = APIRouter(prefix="/api/gd-view", tags=["WeBOC — GD View"])
item_details_router = APIRouter(prefix="/api/item-details", tags=["WeBOC — Item Details"])
into_bond_gd_router = APIRouter(prefix="/api/into-bond-gd", tags=["WeBOC — Into-Bond GD"])
ex_bond_gd_router = APIRouter(prefix="/api/ex-bond-gd", tags=["WeBOC — Ex-Bond GD"])
weboc_router = APIRouter(prefix="/api/weboc", tags=["WeBOC"])


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


# ---------------------------------------------------------------------------
# GD View
# ---------------------------------------------------------------------------
@gd_view_router.post("/upload-and-extract")
async def gd_view_upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_GD_VIEW,
               user_id=current_user.user_id, override_reason=override_reason)
    ext, filename = _check_ext(file)
    existing = (db.query(GoodsDeclaration)
                  .filter(GoodsDeclaration.shipment_id == shipment_id)
                  .order_by(GoodsDeclaration.gd_id.desc()).first())
    existing_id = existing.gd_id if existing else None
    existing_dict = svc.get_gd_view_current(existing) if existing else {}

    file_contents = await file.read()
    staged_name, stage_path = svc.stage_attachment_bytes(file_contents, filename)

    extracted, extraction_error = safe_extract(
        extract_gd_view, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"GD View, shipment_id={shipment_id}, file={filename}")

    if extraction_error:
        logger.warning("GD View shipment=%s: extraction failed, staged for manual entry.", shipment_id)
        return {
            "staged_file": staged_name,
            "original_filename": filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": existing_dict,
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": existing is not None,
        }

    meter_document_accepted(file_path=stage_path)
    warnings = cross_check_gd_view(extracted, shipment_id, db)
    if warnings:
        logger.warning(f"GD View cross-check shipment={shipment_id}: {warnings}")

    extracted["gd_type"] = svc._resolve_gd_type_from_data(extracted)
    bonded = extracted.get("bonded_qty_mt") or extracted.get("gross_weight_mt") or extracted.get("net_weight_mt")
    extracted["bonded_qty_mt"] = float(bonded) if bonded is not None else None

    return {
        "staged_file": staged_name,
        "original_filename": filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
    }


@gd_view_router.post("/")
def gd_view_save(
    data: GDViewSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = svc.save_gd_view(data, current_user.user_id, db)
    logger.info(f"GD View saved: gd_id={gd.gd_id}, type={gd.gd_type}, eta={gd.eta}, "
                f"late_filed={gd.late_filed}, status={gd.status}")
    return {"success": True, "gd_id": gd.gd_id, "status": gd.status,
            "gd_type": gd.gd_type, "late_filed": bool(gd.late_filed)}


@gd_view_router.get("/{gd_id}/document")
def gd_view_document(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    att = svc.get_gd_attachment_file(gd_id, "GD_VIEW", db)
    return document_file_response(str(att.file_path), att.filename)


# ---------------------------------------------------------------------------
# Item Details
# ---------------------------------------------------------------------------
@item_details_router.post("/upload-and-extract")
async def item_details_upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_ITEM_DETAILS,
               user_id=current_user.user_id, override_reason=override_reason)
    ext, filename = _check_ext(file)
    existing = (db.query(GoodsDeclaration)
                  .filter(GoodsDeclaration.shipment_id == shipment_id)
                  .order_by(GoodsDeclaration.gd_id.desc()).first())
    existing_id = existing.gd_id if existing else None
    existing_dict = svc.get_item_details_current(existing, db) if existing else {}

    file_contents = await file.read()
    staged_name, stage_path = svc.stage_attachment_bytes(file_contents, filename)

    extracted, extraction_error = safe_extract(
        extract_item_details, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Item Details, shipment_id={shipment_id}, file={filename}")

    if extraction_error:
        logger.warning("Item Details shipment=%s: extraction failed, staged for manual entry.", shipment_id)
        return {
            "staged_file": staged_name,
            "original_filename": filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": existing_dict,
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": existing is not None,
        }

    meter_document_accepted(file_path=stage_path)
    warnings = cross_check_item_details(extracted, shipment_id, db)
    if warnings:
        logger.warning(f"Item Details cross-check shipment={shipment_id}: {warnings}")

    return {
        "staged_file": staged_name,
        "original_filename": filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
    }


@item_details_router.post("/")
def item_details_save(
    data: ItemDetailsSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = svc.save_item_details(data, current_user.user_id, db)
    logger.info(f"Item Details saved: gd_id={gd.gd_id}, items={len(gd.items)}")
    return {"success": True, "gd_id": gd.gd_id, "status": gd.status,
            "items": len(gd.items)}


@item_details_router.get("/{gd_id}/document")
def item_details_document(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    att = svc.get_gd_attachment_file(gd_id, "ITEM_DETAILS", db)
    return document_file_response(str(att.file_path), att.filename)


# ---------------------------------------------------------------------------
# WeBOC tab summary + ex-bond entries
# ---------------------------------------------------------------------------
def _gd_view_ready(gd, db: Session) -> bool:
    if gd.gd_view_uploaded or gd.gd_number:
        return True
    try:
        svc.get_gd_attachment_file(gd.gd_id, "GD_VIEW", db)
        return True
    except NotFoundError:
        return False


@weboc_router.get("/{gd_id}/summary")
def weboc_summary(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    gd = svc.get_gd_or_error(gd_id, db)

    duties = {k: float(getattr(gd, k)) if getattr(gd, k) is not None else None for k in (
        "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr", "additional_customs_duty_pkr",
        "additional_sales_tax_pkr", "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr",
        "total_duties_pkr")}

    items = [{
        "item_id": it.item_id, "item_number": it.item_number, "hs_code": it.hs_code,
        "goods_description": it.goods_description,
        "quantity": float(it.quantity) if it.quantity is not None else None,
        "unit": it.unit or it.unit_type,
        "declared_quantity": float(it.declared_quantity) if it.declared_quantity is not None else None,
        "assessed_quantity": float(it.assessed_quantity) if it.assessed_quantity is not None else None,
        "sro_no": it.sro_no, "quota_reference": it.quota_reference,
        "country_of_origin": it.country_of_origin,
    } for it in sorted(gd.items, key=lambda x: x.item_number or 0)]

    checks = []
    if gd.shipment_id:
        gv = {
            "declaration_type": gd.declaration_type_raw or gd.gd_type,
            "bl_number": gd.bl_number, "importer_name": gd.importer_name,
            "eta": gd.eta.isoformat() if gd.eta else None,
            "net_weight_mt": float(gd.net_weight_mt) if gd.net_weight_mt is not None else None,
            "gross_weight_mt": float(gd.gross_weight_mt) if gd.gross_weight_mt is not None else None,
            "section82_penalty_pkr": float(gd.section82_penalty_pkr) if gd.section82_penalty_pkr is not None else None,
        }
        if _gd_view_ready(gd, db):
            checks += cross_check_gd_view(gv, gd.shipment_id, db)
        if gd.item_details_uploaded and items:
            checks += cross_check_item_details({
                "importer_name": gd.importer_name,
                "items": [{"item_number": i["item_number"], "hs_code": i["hs_code"],
                           "quantity": i["quantity"], "unit": i["unit"],
                           "sro_no": i["sro_no"], "quota_reference": i["quota_reference"]}
                          for i in items],
                "net_weight_mt": float(gd.net_weight_mt) if gd.net_weight_mt is not None else None,
            }, gd.shipment_id, db)

    into_bond_uploaded = bool(getattr(gd, "into_bond_gd_uploaded", False))
    if not into_bond_uploaded:
        try:
            svc.get_gd_attachment_file(gd.gd_id, "INTO_BOND_GD", db)
            into_bond_uploaded = True
        except NotFoundError:
            pass

    kgtl = kgtl_summary(gd, db)

    return {
        "gd_id": gd.gd_id, "shipment_id": gd.shipment_id, "status": gd.status,
        "gd_view": {
            "uploaded": _gd_view_ready(gd, db),
            "gd_number": gd.gd_number, "filing_date": gd.filing_date.isoformat() if gd.filing_date else None,
            "eta": gd.eta.isoformat() if gd.eta else None,
            "declaration_type_raw": gd.declaration_type_raw,
            "gd_type": gd.gd_type,
            "declaration_unknown": (gd.gd_type or "UNKNOWN") == "UNKNOWN",
            "importer_name": gd.importer_name, "importer_ntn": gd.importer_ntn,
            "vessel_name": gd.vessel_name, "bl_number": gd.bl_number,
            "custom_office": gd.custom_office,
            "gross_weight_mt": float(gd.gross_weight_mt) if gd.gross_weight_mt is not None else None,
            "net_weight_mt": float(gd.net_weight_mt) if gd.net_weight_mt is not None else None,
            "package_count": gd.package_count, "package_type": gd.package_type,
            "hs_code": gd.hs_code, "goods_description": gd.goods_description,
        },
        "duties": duties,
        "charges_breakdown": gd.charges_breakdown or [],
        "penalty": {
            "section82_penalty_pkr": float(gd.section82_penalty_pkr) if gd.section82_penalty_pkr is not None else None,
            "late_filed": bool(gd.late_filed),
            "has_penalty": (gd.section82_penalty_pkr or 0) > 0,
        },
        "filing": filing_deadline(gd),
        "bond": bond_summary(gd, db),
        "into_bond_gd": {
            "uploaded": into_bond_uploaded,
            "gd_number": gd.gd_number,
            "filing_date": gd.filing_date.isoformat() if gd.filing_date else None,
            "gross_weight_mt": float(gd.gross_weight_mt) if gd.gross_weight_mt is not None else None,
            "net_weight_mt": float(gd.net_weight_mt) if gd.net_weight_mt is not None else None,
            "bonded_qty_mt": float(gd.bonded_qty_mt) if gd.bonded_qty_mt is not None else None,
        },
        "item_details": {"uploaded": bool(gd.item_details_uploaded), "items": items},
        "sro": gd_sro_usage(gd, db),
        "kgtl": kgtl,
        "quantity_reconciliation": quantity_reconciliation(gd, db, kgtl),
        "gd_closed": gd_is_closed(gd),
        "checks": checks,
    }


@weboc_router.post("/{gd_id}/ex-bond")
def add_ex_bond(
    gd_id: int,
    data: ExBondEntrySave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    e = svc.add_ex_bond_entry(gd_id, data, current_user.user_id, db)
    gd = svc.get_gd_or_error(gd_id, db)
    logger.info(f"Ex-bond entry added: into_bond_gd={gd.gd_id}, qty={e.quantity_mt}, gd={e.gd_number}")
    return {"success": True, "entry_id": e.entry_id, "bond": bond_summary(gd, db),
            "over_lifted": data.force, "status": gd.status}


@weboc_router.put("/ex-bond/{entry_id}")
def update_ex_bond(
    entry_id: int,
    data: ExBondEntrySave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    e = svc.update_ex_bond_entry(entry_id, data, db)
    gd = svc.get_gd_or_error(e.into_bond_gd_id, db)
    return {"success": True, "entry_id": entry_id, "bond": bond_summary(gd, db),
            "status": gd.status}


@weboc_router.delete("/ex-bond/{entry_id}")
def delete_ex_bond(
    entry_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = svc.delete_ex_bond_entry(entry_id, db, deleted_by=current_user.user_id)
    return {"success": True, "entry_id": entry_id,
            "bond": bond_summary(gd, db) if gd else None}


@weboc_router.get("/{gd_id}/kgtl")
def get_kgtl_weighments(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    gd = svc.get_gd_or_error(gd_id, db)
    return {"kgtl": kgtl_summary(gd, db), "gd_closed": gd_is_closed(gd),
            "gd_status": gd.status, "gd_type": gd.gd_type}


@weboc_router.post("/{gd_id}/kgtl")
def add_kgtl_weighment(
    gd_id: int,
    data: GdKgtlWeighmentSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    row = svc.add_kgtl_weighment(gd_id, data, current_user.user_id, db)
    gd = svc.get_gd_or_error(gd_id, db)
    logger.info(f"KGTL weighment added: gd={gd.gd_id}, vehicle={row.vehicle_number}")
    return {"success": True, "weighment_id": row.weighment_id, "kgtl": kgtl_summary(gd, db)}


@weboc_router.put("/kgtl/{weighment_id}")
def update_kgtl_weighment(
    weighment_id: int,
    data: GdKgtlWeighmentSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    row = svc.update_kgtl_weighment(weighment_id, data, db)
    gd = svc.get_gd_or_error(row.gd_id, db)
    return {"success": True, "weighment_id": weighment_id, "kgtl": kgtl_summary(gd, db)}


@weboc_router.delete("/kgtl/{weighment_id}")
def delete_kgtl_weighment(
    weighment_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = svc.delete_kgtl_weighment(weighment_id, db, deleted_by=current_user.user_id)
    return {"success": True, "weighment_id": weighment_id,
            "kgtl": kgtl_summary(gd, db) if gd else None}


@weboc_router.put("/{gd_id}/bond-penalty")
def set_bond_penalty(
    gd_id: int,
    data: BondPenaltySave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    svc.set_bond_penalty(gd_id, data, current_user.user_id, db)
    gd = svc.get_gd_or_error(gd_id, db)
    logger.info(f"Bond penalty recorded: gd={gd.gd_id}, PKR {data.penalty_pkr}, source={data.source}")
    return {"success": True, "bond": bond_summary(gd, db)}


@weboc_router.delete("/{gd_id}/bond-penalty")
def clear_bond_penalty(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    svc.clear_bond_penalty(gd_id, db)
    gd = svc.get_gd_or_error(gd_id, db)
    return {"success": True, "bond": bond_summary(gd, db)}


@weboc_router.put("/{gd_id}/declaration-type")
def set_declaration_type(
    gd_id: int,
    data: DeclarationTypeOverride,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    svc.set_declaration_type(gd_id, data, db)
    gd = svc.get_gd_or_error(gd_id, db)
    return {"success": True, "gd_id": gd_id, "gd_type": gd.gd_type,
            "bonded_qty_mt": float(gd.bonded_qty_mt) if gd.bonded_qty_mt is not None else None,
            "bond": bond_summary(gd, db)}


# ---------------------------------------------------------------------------
# Into-Bond GD (filed IB document — step 2 after GD View for INTO_BOND shipments)
# ---------------------------------------------------------------------------
@into_bond_gd_router.post("/upload-and-extract")
async def into_bond_gd_upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_INTO_BOND_GD,
               user_id=current_user.user_id, override_reason=override_reason)
    ext, filename = _check_ext(file)
    existing = (db.query(GoodsDeclaration)
                  .filter(GoodsDeclaration.shipment_id == shipment_id)
                  .order_by(GoodsDeclaration.gd_id.desc()).first())
    if existing is None or not _gd_view_ready(existing, db):
        raise HTTPException(status_code=400,
                            detail="Upload and save the GD View first — it identifies the Into-Bond type.")
    if (existing.gd_type or "") not in ("INTO_BOND", "UNKNOWN"):
        raise HTTPException(status_code=400,
                            detail="This shipment's GD is not Into-Bond type.")

    existing_id = existing.gd_id
    existing_dict = svc.get_into_bond_gd_current(existing)

    file_contents = await file.read()
    staged_name, stage_path = svc.stage_attachment_bytes(file_contents, filename)

    extracted, extraction_error = safe_extract(
        extract_gd, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Into-Bond GD, shipment_id={shipment_id}, file={filename}")

    if extraction_error:
        logger.warning("Into-Bond GD shipment=%s: extraction failed — manual entry.", shipment_id)
        return {
            "staged_file": staged_name,
            "original_filename": filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": existing_dict,
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": bool(existing.into_bond_gd_uploaded),
        }

    meter_document_accepted(file_path=stage_path)
    extracted["gd_type"] = "INTO_BOND"
    bonded = extracted.get("bonded_qty_mt") or extracted.get("gross_weight_mt") or extracted.get("net_weight_mt")
    extracted["bonded_qty_mt"] = float(bonded) if bonded is not None else None
    extracted["declaration_type"] = extracted.get("declaration_type") or "IB"
    warnings = []
    if svc._resolve_gd_type_from_data(extracted) != "INTO_BOND":
        warnings.append("This document does not look like an Into-Bond (IB) GD — please verify.")

    return {
        "staged_file": staged_name,
        "original_filename": filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
    }


@into_bond_gd_router.post("/")
def into_bond_gd_save(
    data: IntoBondGDSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    gd = svc.save_into_bond_gd(data, current_user.user_id, db)
    logger.info(f"Into-Bond GD saved: gd_id={gd.gd_id}, number={gd.gd_number}, "
                f"bonded={gd.bonded_qty_mt}, status={gd.status}")
    return {"success": True, "gd_id": gd.gd_id, "status": gd.status,
            "bond": bond_summary(gd, db)}


@into_bond_gd_router.get("/{gd_id}/document")
def into_bond_gd_document(
    gd_id: int,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    att = svc.get_gd_attachment_file(gd_id, "INTO_BOND_GD", db)
    return document_file_response(str(att.file_path), att.filename)


# ---------------------------------------------------------------------------
# Ex-Bond GD (filed EB/XB documents — one per lifting, updates settlement)
# ---------------------------------------------------------------------------
@ex_bond_gd_router.post("/upload-and-extract")
async def ex_bond_gd_upload_and_extract(
    request: Request,
    shipment_id: int = Query(...),
    override_reason: str | None = Query(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    check_gate(db, request, shipment_id, ACTION_UPLOAD_EX_BOND_GD,
               user_id=current_user.user_id, override_reason=override_reason)
    ext, filename = _check_ext(file)
    existing = (db.query(GoodsDeclaration)
                  .filter(GoodsDeclaration.shipment_id == shipment_id)
                  .order_by(GoodsDeclaration.gd_id.desc()).first())
    if existing is None or (existing.gd_type or "") != "INTO_BOND":
        raise HTTPException(status_code=400, detail="Ex-Bond GDs apply only to Into-Bond shipments.")

    into_bond_uploaded = bool(getattr(existing, "into_bond_gd_uploaded", False))
    if not into_bond_uploaded:
        try:
            svc.get_gd_attachment_file(existing.gd_id, "INTO_BOND_GD", db)
            into_bond_uploaded = True
        except NotFoundError:
            pass

    if not into_bond_uploaded:
        raise HTTPException(status_code=400,
                            detail="Upload and save the Into-Bond (IB) GD first to start the process.")

    existing_id = existing.gd_id
    file_contents = await file.read()
    staged_name, stage_path = svc.stage_attachment_bytes(file_contents, filename)

    extracted, extraction_error = safe_extract(
        extract_gd, stage_path, settings.ANTHROPIC_API_KEY,
        doc_label=f"Ex-Bond GD, shipment_id={shipment_id}, file={filename}")

    if extraction_error:
        logger.warning("Ex-Bond GD shipment=%s: extraction failed — manual entry.", shipment_id)
        return {
            "staged_file": staged_name,
            "original_filename": filename,
            "existing_id": existing_id,
            "is_pdf": ext == ".pdf",
            "extracted": svc.get_ex_bond_gd_current(existing),
            "warnings": [],
            "extraction_failed": True,
            "extraction_message": extraction_error,
            "had_previous_data": False,
        }

    meter_document_accepted(file_path=stage_path)
    lift = svc._lift_qty_from_extracted(extracted)
    extracted["quantity_mt"] = float(lift) if lift is not None else None
    extracted["gd_type"] = "EX_BOND"
    extracted["declaration_type"] = extracted.get("declaration_type") or "EX"
    svc.normalize_gd_filing_date(extracted)

    warnings = []
    if svc.classify_declaration(extracted.get("declaration_type")) != "EX_BOND" and \
            svc.classify_from_gd_number(extracted.get("gd_number")) != "EX_BOND":
        warnings.append("This document does not look like an Ex-Bond (EB/XB) GD — please verify.")

    if lift and bond_summary(existing, db).get("bonded_qty_mt"):
        exceeds, remaining, over_by = svc.ex_bond_would_exceed(existing, db, lift)
        if exceeds:
            warnings.append(
                f"This lifting of {float(lift):,.3f} MT exceeds the bonded gross by "
                f"{over_by:,.3f} MT ({remaining:,.3f} MT was remaining). "
                f"Correct the quantity on the verify screen if needed.")

    return {
        "staged_file": staged_name,
        "original_filename": filename,
        "existing_id": existing_id,
        "is_pdf": ext == ".pdf",
        "extracted": extracted,
        "warnings": warnings,
    }


@ex_bond_gd_router.post("/")
def ex_bond_gd_save(
    data: ExBondGDSave,
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(require_min_role("ADMIN", "MANAGER", "OPERATOR")),
):
    e = svc.save_ex_bond_gd(data, current_user.user_id, db)
    gd = svc.get_gd_or_error(e.into_bond_gd_id, db)
    logger.info(f"Ex-Bond GD saved: into_bond_gd={gd.gd_id}, qty={e.quantity_mt}, entry={e.entry_id}")
    # Calculate exceeds for response
    exceeds, _, _ = svc.ex_bond_would_exceed(gd, db, e.quantity_mt, exclude_entry_id=e.entry_id)
    return {"success": True, "entry_id": e.entry_id, "bond": bond_summary(gd, db),
            "status": gd.status, "over_lifted": exceeds}


@ex_bond_gd_router.get("/{gd_id}/document")
def ex_bond_gd_document(
    gd_id: int,
    attachment_id: int = Query(...),
    db: Session = Depends(get_tenant_db),
    current_user: User = Depends(get_current_user),
):
    att = (db.query(svc.GDAttachment)
             .filter(svc.GDAttachment.attachment_id == attachment_id,
                     svc.GDAttachment.gd_id == gd_id,
                     svc.GDAttachment.kind == "EX_BOND_GD")
             .first())
    if not att or not att.file_path or not os.path.exists(att.file_path):
        raise HTTPException(status_code=404, detail="Document not found")
    return document_file_response(str(att.file_path), att.filename)
