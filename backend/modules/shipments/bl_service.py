"""Bill of Lading business logic - CRUD, LC/shipment sync, demurrage assembly.
Extracted from modules/shipments/bl_router.py.
"""
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.exceptions import ConflictError, NotFoundError, ValidationError
from models.database_models import BillOfLading, DemurrageConfig, LCMaster, Shipment
from modules.shipments.bl_schemas import BLSave
from modules.shipments.demurrage_service import compute_demurrage
from modules.shipments.container_detention_service import compute_container_detention, resolve_bl_type
from modules.shipments.eta_calc import estimate_eta
from infrastructure.normalization.normalization_service import normalize_bl_parties
from utils.staging import promote_staged, replace_document_path

BL_STAGE_SUBDIR = "bl_documents"
BL_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def get_demurrage_config(db: Session) -> Optional[DemurrageConfig]:
    return db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()


def bl_to_dict(bl: BillOfLading, config: Optional[DemurrageConfig] = None, db: Optional[Session] = None) -> dict:
    return {
        "bl_id": bl.bl_id,
        "lc_id": bl.lc_id,
        "lc_number": bl.lc.lc_number if bl.lc else None,
        "document_filename": bl.document_filename,
        "document_path": bl.document_path,
        "source": bl.source,
        "bl_number": bl.bl_number,
        "bl_date": bl.bl_date.isoformat() if bl.bl_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": bl.upload_date.isoformat() if bl.upload_date else None,
        "bl_issue_place": bl.bl_issue_place,
        "original_bl_count": bl.original_bl_count,
        "shipper_name": bl.shipper_name,
        "shipper_address": bl.shipper_address,
        "consignee": bl.consignee,
        "notify_party": bl.notify_party,
        "carrier_name": bl.carrier_name,
        "shipping_agent": bl.shipping_agent,
        "vessel_name": bl.vessel_name,
        "voyage_number": bl.voyage_number,
        "pre_carriage_by": bl.pre_carriage_by,
        "place_of_receipt": bl.place_of_receipt,
        "port_of_loading": bl.port_of_loading,
        "port_of_discharge": bl.port_of_discharge,
        "final_destination": bl.final_destination,
        "freight_payable_at": bl.freight_payable_at,
        "shipping_marks": bl.shipping_marks,
        "package_count": bl.package_count,
        "package_type": bl.package_type,
        "goods_description": bl.goods_description,
        "gross_weight_mt": float(bl.gross_weight_mt) if bl.gross_weight_mt else None,
        "net_weight_mt": float(bl.net_weight_mt) if bl.net_weight_mt else None,
        "measurement_m3": float(bl.measurement_m3) if bl.measurement_m3 else None,
        "applicant_ntn": bl.applicant_ntn,
        "freight_terms": bl.freight_terms,
        "shipped_on_board_clause": bl.shipped_on_board_clause,
        "status": bl.status,
        "notes": bl.notes,
        # Demurrage raw fields
        "demurrage_start_date": bl.demurrage_start_date.isoformat() if bl.demurrage_start_date else None,
        "free_days": bl.free_days,
        "demurrage_total_amount": float(bl.demurrage_total_amount) if bl.demurrage_total_amount is not None else None,
        "demurrage_currency": bl.demurrage_currency,
        "demurrage_cleared_date": bl.demurrage_cleared_date.isoformat() if bl.demurrage_cleared_date else None,
        "bl_type": bl.bl_type,
        "is_container_bl": bl.bl_type == "CONTAINER",
        # Computed demurrage clock (coil shipments only)
        "demurrage": compute_demurrage(bl, config) if bl.bl_type != "CONTAINER" else {},
        # Detention raw fields (container BLs only — separate clock from coil demurrage)
        "detention_start_date": bl.detention_start_date.isoformat() if bl.detention_start_date else None,
        "detention_free_days": bl.detention_free_days,
        "detention_end_date": bl.detention_end_date.isoformat() if bl.detention_end_date else None,
        "detention_total_amount": float(bl.detention_total_amount) if bl.detention_total_amount is not None else None,
        "detention_currency": bl.detention_currency,
        "detention_paid_date": bl.detention_paid_date.isoformat() if bl.detention_paid_date else None,
        "detention_remarks": bl.detention_remarks,
        # Computed detention clock (container shipments only)
        "detention": compute_container_detention(bl, config) if bl.bl_type == "CONTAINER" else {},
        "created_at": bl.created_at.isoformat() if bl.created_at else None,
        "updated_at": bl.updated_at.isoformat() if bl.updated_at else None,
        "has_document": bool(bl.document_path and os.path.exists(bl.document_path)),
    }


def sync_lc_master(bl: BillOfLading, db: Session) -> None:
    """Copy key BL fields back to lc_master stubs when BL is verified."""
    if not bl.lc_id:
        return
    lc = db.query(LCMaster).filter(LCMaster.lc_id == bl.lc_id).first()
    if not lc:
        return
    if bl.bl_number:
        lc.bl_number = bl.bl_number
    if bl.vessel_name:
        lc.vessel_name = bl.vessel_name
    if bl.bl_date:
        lc.ship_on_board = bl.bl_date
    if bl.port_of_discharge:
        lc.arrival_port = bl.port_of_discharge
    db.flush()


def sync_shipment_from_bl(bl: BillOfLading, db: Session) -> None:
    """Copy key transport fields from a BL onto its shipment."""
    if not bl.shipment_id:
        return
    s = db.query(Shipment).filter(Shipment.shipment_id == bl.shipment_id).first()
    if not s:
        return
    if bl.vessel_name:
        s.vessel_name = bl.vessel_name
    if bl.voyage_number:
        s.voyage_number = bl.voyage_number
    if bl.port_of_loading:
        s.port_of_loading = bl.port_of_loading
    if bl.port_of_discharge:
        s.port_of_discharge = bl.port_of_discharge
    if bl.bl_date:
        s.bl_date = bl.bl_date
        # ETD defaults to the BL / shipped-on-board date; keep any value the user set manually.
        if not s.etd:
            s.etd = bl.bl_date
        # Auto-estimate ETA (etd + transit_days business days) while it hasn't been confirmed
        # by a human or the KPT tracker yet — see Shipment.eta_source.
        if s.eta_source in (None, "AUTO"):
            new_eta = estimate_eta(s.etd, s.transit_days)
            if new_eta:
                s.eta = new_eta
                s.eta_source = "AUTO"
    if bl.package_count is not None:
        s.total_coils = bl.package_count
    if not s.lc_id and bl.lc_id:
        s.lc_id = bl.lc_id
    if s.status == "PENDING":
        s.status = "DOCUMENTS_RECEIVED"
    db.flush()


def get_bl_or_404(bl_id: int, db: Session) -> BillOfLading:
    bl = db.query(BillOfLading).filter(BillOfLading.bl_id == bl_id).first()
    if not bl:
        raise NotFoundError("BL not found")
    return bl


def check_duplicate_bl_number(db: Session, bl_number: Optional[str], exclude_bl_id: Optional[int]) -> None:
    if not bl_number:
        return
    duplicate = db.query(BillOfLading).filter(
        BillOfLading.bl_number == bl_number,
        BillOfLading.bl_id != exclude_bl_id,
    ).first()
    if duplicate:
        raise ConflictError(
            f"BL number '{bl_number}' already exists (BL ID: {duplicate.bl_id}). "
            f"Each BL number must be unique.")


def apply_bl_fields(bl: BillOfLading, data: BLSave, user_id: int) -> None:
    fields_set = data.model_fields_set
    for field in (
        "bl_number", "bl_issue_place", "shipper_name", "shipper_address", "consignee",
        "notify_party", "carrier_name", "shipping_agent", "vessel_name", "voyage_number",
        "pre_carriage_by", "place_of_receipt", "port_of_loading", "port_of_discharge",
        "final_destination", "freight_payable_at", "shipping_marks", "package_type",
        "goods_description", "bl_type", "applicant_ntn", "freight_terms", "shipped_on_board_clause",
        "notes", "source", "demurrage_currency",
        "original_bl_count", "package_count", "free_days",
        "gross_weight_mt", "net_weight_mt", "measurement_m3", "demurrage_total_amount",
        "demurrage_start_date", "demurrage_cleared_date",
        "detention_currency", "detention_remarks", "detention_free_days",
        "detention_total_amount", "detention_start_date", "detention_end_date",
        "detention_paid_date",
        "upload_date",
        "lc_id", "shipment_id",
    ):
        if field in fields_set:
            setattr(bl, field, getattr(data, field))
    # bl_date can only be set, never cleared via this endpoint (a BL's shipment date is a
    # real historical fact, not something to blank out from a form save).
    if data.bl_date is not None:
        bl.bl_date = data.bl_date

    bl.updated_by = user_id
    bl.updated_at = datetime.utcnow()


def apply_staged_document(bl: BillOfLading, data: BLSave, upload_dir: Optional[str] = None):
    if not data.staged_file:
        return
    dest, orig = promote_staged(
        BL_STAGE_SUBDIR, data.staged_file, bl.bl_id,
        data.original_filename, BL_ALLOWED_EXTENSIONS,
        perm_dir=upload_dir,
    )
    if dest:
        replace_document_path(bl, dest, orig)
        bl.file_pending = False
    if data.raw_extracted_data is not None:
        bl.raw_extracted_data = data.raw_extracted_data


def _sync_file_pending(bl: BillOfLading) -> None:
    bl.file_pending = not (bl.document_path and os.path.exists(bl.document_path))


BL_SCALAR_FIELDS = [
    "bl_number", "vessel_name", "voyage_number", "port_of_loading", "port_of_discharge",
    "gross_weight_mt", "net_weight_mt", "package_count", "goods_description", "bl_type",
]


def create_bl(data: BLSave, db: Session, user_id: int,
              upload_dir: Optional[str] = None) -> BillOfLading:
    check_duplicate_bl_number(db, data.bl_number, data.bl_id)

    if data.bl_id:
        bl = get_bl_or_404(data.bl_id, db)
    else:
        bl = BillOfLading(
            source="UPLOADED" if data.staged_file else "MANUAL",
            created_by=user_id,
        )
        db.add(bl)
        db.flush()

    apply_bl_fields(bl, data, user_id)

    overwrites = set(data.confirm_overwrites or [])
    if overwrites and bl.field_sources:
        for field in overwrites:
            if hasattr(data, field) and field in data.model_fields_set:
                setattr(bl, field, getattr(data, field))
                sources = dict(bl.field_sources or {})
                sources[field] = "EXTRACTED"
                bl.field_sources = sources

    apply_staged_document(bl, data, upload_dir=upload_dir)
    _sync_file_pending(bl)
    if not data.staged_file and bl.source == "MANUAL":
        bl.file_pending = True
        sources = dict(bl.field_sources or {})
        for field in BL_SCALAR_FIELDS:
            val = getattr(bl, field, None)
            if val not in (None, ""):
                sources[field] = sources.get(field) or "MANUAL"
        bl.field_sources = sources or None
    normalize_bl_parties(bl, db)
    if not bl.bl_type:
        bl.bl_type = resolve_bl_type(bl.raw_extracted_data, bl, db)

    if not bl.status or bl.status == "PENDING_REVIEW":
        bl.status = "VERIFIED"

    db.commit()
    db.refresh(bl)

    if bl.status == "VERIFIED":
        sync_lc_master(bl, db)
        sync_shipment_from_bl(bl, db)
        if bl.shipment_id:
            from infrastructure.activity.activity_service import log_activity
            from modules.shipments.services import touch_docs_reception

            action = "MANUAL_STUB_CREATED" if bl.file_pending else "UPLOAD"
            if data.staged_file and not bl.file_pending:
                log_activity(db, bl.shipment_id, user_id, "FILE_ATTACHED", doc_type="Bill of Lading")
            log_activity(db, bl.shipment_id, user_id, action, doc_type="Bill of Lading")
            touch_docs_reception(bl.shipment_id, db)
        db.commit()

    return bl


def update_bl(bl_id: int, data: BLSave, db: Session, user_id: int) -> BillOfLading:
    bl = get_bl_or_404(bl_id, db)
    check_duplicate_bl_number(db, data.bl_number, bl_id)

    apply_bl_fields(bl, data, user_id)
    normalize_bl_parties(bl, db)
    if not bl.bl_type:
        bl.bl_type = resolve_bl_type(bl.raw_extracted_data, bl, db)
    if bl.shipment_id:
        from infrastructure.activity.activity_service import log_activity
        log_activity(db, bl.shipment_id, user_id, "EDIT", doc_type="Bill of Lading")
    db.commit()
    db.refresh(bl)

    if bl.status == "VERIFIED":
        sync_lc_master(bl, db)
        db.commit()

    return bl


def link_bl_to_lc(bl_id: int, lc_id: int, db: Session, user_id: int) -> LCMaster:
    bl = get_bl_or_404(bl_id, db)
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise NotFoundError("LC not found")

    bl.lc_id = lc_id
    bl.updated_by = user_id
    db.commit()

    if bl.status == "VERIFIED":
        sync_lc_master(bl, db)
        db.commit()

    return lc


def update_bl_status(bl_id: int, status: str, notes: Optional[str], db: Session, user_id: int) -> BillOfLading:
    bl = get_bl_or_404(bl_id, db)
    bl.status = status
    if notes:
        bl.notes = notes
    bl.updated_by = user_id
    db.commit()

    if status == "VERIFIED":
        sync_lc_master(bl, db)
        db.commit()

    return bl


def delete_bl(bl_id: int, db: Session) -> None:
    bl = get_bl_or_404(bl_id, db)
    if bl.document_path and os.path.exists(bl.document_path):
        try:
            os.remove(bl.document_path)
        except OSError:
            pass
    db.delete(bl)
    db.commit()
