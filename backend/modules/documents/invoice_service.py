"""Business logic for Commercial Invoices, extracted from
modules/documents/invoice_router.py as part of the Phase 4 module rollout.
"""
import os
import shutil
from datetime import datetime
from typing import Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session, joinedload

from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import CommercialInvoice, InvoiceLineItem, Shipment, LCMaster
from modules.documents.invoice_schemas import InvoiceSave, STR_FIELDS, DEC_FIELDS
from infrastructure.normalization.normalization_service import normalize_invoice_parties
from utils.uploads import safe_upload_path, tenant_doc_dir
from utils.staging import promote_staged, replace_document_path

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
STAGE_SUBDIR = "invoice_documents"


def _upload_dir(tenant_schema: str | None) -> str | None:
    return tenant_doc_dir(tenant_schema, STAGE_SUBDIR) if tenant_schema else None


def save_file(upload: UploadFile, invoice_id: int, tenant_schema: str) -> str:
    upload_dir = tenant_doc_dir(tenant_schema, STAGE_SUBDIR)
    dest = safe_upload_path(upload_dir, invoice_id, upload.filename, ALLOWED_EXTENSIONS)
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def _cap(value: str, field: str) -> str:
    """Truncate a string to its DB column's max length so an over-long extracted
    value (e.g. an hs_code carrying multiple codes) can never crash the save with a
    StringDataRightTruncation error. Text/unbounded columns have length=None."""
    col = CommercialInvoice.__table__.columns.get(field)
    limit = getattr(getattr(col, "type", None), "length", None) if col is not None else None
    if limit and len(value) > limit:
        return value[:limit]
    return value


def apply_invoice_fields(inv: CommercialInvoice, data: InvoiceSave, user_id: int):
    fields_set = data.model_fields_set

    for f in STR_FIELDS:
        v = getattr(data, f)
        if f in fields_set and v is not None:
            val = str(v).strip() or None
            setattr(inv, f, _cap(val, f) if val else None)

    for f in DEC_FIELDS:
        if f in fields_set:
            setattr(inv, f, getattr(data, f))

    if "total_coils" in fields_set:
        inv.total_coils = data.total_coils

    if data.invoice_date:
        inv.invoice_date = data.invoice_date

    if "upload_date" in fields_set:
        inv.upload_date = data.upload_date

    inv.updated_by = user_id
    inv.updated_at = datetime.utcnow()


def replace_line_items(inv: CommercialInvoice, items: Optional[list], db: Session):
    db.query(InvoiceLineItem).filter(InvoiceLineItem.invoice_id == inv.invoice_id).delete()
    for it in (items or []):
        db.add(InvoiceLineItem(
            invoice_id=inv.invoice_id, shipment_id=inv.shipment_id,
            item_number=it.item_number,
            size_thickness_mm=it.size_thickness_mm,
            size_width_mm=it.size_width_mm,
            quantity_mt=it.quantity_mt,
            net_weight_mt=it.net_weight_mt,
            gross_weight_mt=it.gross_weight_mt,
            number_of_coils=it.number_of_coils,
            unit_price_usd=it.unit_price_usd,
            line_amount_usd=it.line_amount_usd,
        ))


def sync_shipment_from_invoice(inv: CommercialInvoice, db: Session) -> Optional[str]:
    """Update shipment totals + delivered qty + variance. Returns a warning string or None."""
    if not inv.shipment_id:
        return None
    s = db.query(Shipment).filter(Shipment.shipment_id == inv.shipment_id).first()
    if not s:
        return None

    if inv.total_coils is not None:
        s.total_coils = inv.total_coils
    if inv.total_net_weight_mt is not None:
        s.total_net_weight_mt = inv.total_net_weight_mt
        s.delivered_quantity_mt = inv.total_net_weight_mt
    if inv.total_gross_weight_mt is not None:
        s.total_gross_weight_mt = inv.total_gross_weight_mt
    if inv.vessel_name and not s.vessel_name:
        s.vessel_name = inv.vessel_name
    if inv.port_of_loading and not s.port_of_loading:
        s.port_of_loading = inv.port_of_loading
    if inv.port_of_discharge and not s.port_of_discharge:
        s.port_of_discharge = inv.port_of_discharge

    # variance (delivered - expected); negative = short shipment
    if s.expected_quantity_mt is not None and s.delivered_quantity_mt is not None:
        s.quantity_variance_mt = s.delivered_quantity_mt - s.expected_quantity_mt

    if s.status == "PENDING":
        s.status = "DOCUMENTS_RECEIVED"

    # verify documentary credit number against the LC
    warning = None
    if inv.documentary_credit_number and s.lc_id:
        lc = db.query(LCMaster).filter(LCMaster.lc_id == s.lc_id).first()
        if lc and lc.lc_number and inv.documentary_credit_number.strip().upper() not in (lc.lc_number.upper(), ):
            # soft check — only warn if clearly different and not a substring match
            if lc.lc_number.upper() not in inv.documentary_credit_number.upper() and \
               inv.documentary_credit_number.upper() not in lc.lc_number.upper():
                warning = (f"Invoice credit number '{inv.documentary_credit_number}' does not match "
                           f"this shipment's LC '{lc.lc_number}'.")
    db.flush()
    return warning


def to_dict(inv: CommercialInvoice) -> dict:
    return {
        "invoice_id": inv.invoice_id, "shipment_id": inv.shipment_id, "lc_id": inv.lc_id,
        "document_filename": inv.document_filename,
        "has_document": bool(inv.document_path and os.path.exists(inv.document_path)),
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        # Manual upload-date override — the UI falls back to created_at when this is unset.
        "upload_date": inv.upload_date.isoformat() if inv.upload_date else None,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "documentary_credit_number": inv.documentary_credit_number,
        "seller_name": inv.seller_name, "seller_address": inv.seller_address,
        "buyer_name": inv.buyer_name, "buyer_address": inv.buyer_address,
        "goods_description": inv.goods_description, "grade": inv.grade,
        "hs_code": inv.hs_code, "country_of_origin": inv.country_of_origin,
        "incoterms": inv.incoterms, "currency": inv.currency,
        "unit_price_usd": float(inv.unit_price_usd) if inv.unit_price_usd else None,
        "total_net_weight_mt": float(inv.total_net_weight_mt) if inv.total_net_weight_mt else None,
        "total_gross_weight_mt": float(inv.total_gross_weight_mt) if inv.total_gross_weight_mt else None,
        "total_coils": inv.total_coils,
        "total_amount_usd": float(inv.total_amount_usd) if inv.total_amount_usd else None,
        "vessel_name": inv.vessel_name, "voyage_number": inv.voyage_number,
        "port_of_loading": inv.port_of_loading, "port_of_discharge": inv.port_of_discharge,
        "status": inv.status,
        "line_items": [{
            "item_number": li.item_number,
            "size_thickness_mm": float(li.size_thickness_mm) if li.size_thickness_mm else None,
            "size_width_mm": float(li.size_width_mm) if li.size_width_mm else None,
            "quantity_mt": float(li.quantity_mt) if li.quantity_mt else None,
            "net_weight_mt": float(li.net_weight_mt) if li.net_weight_mt else None,
            "gross_weight_mt": float(li.gross_weight_mt) if li.gross_weight_mt else None,
            "number_of_coils": li.number_of_coils,
            "unit_price_usd": float(li.unit_price_usd) if li.unit_price_usd else None,
            "line_amount_usd": float(li.line_amount_usd) if li.line_amount_usd else None,
        } for li in sorted(inv.line_items, key=lambda x: x.item_number or 0)],
    }


def get_invoice_or_404(db: Session, invoice_id: int, with_items: bool = False) -> CommercialInvoice:
    q = db.query(CommercialInvoice)
    if with_items:
        q = q.options(joinedload(CommercialInvoice.line_items))
    inv = q.filter(CommercialInvoice.invoice_id == invoice_id).first()
    if not inv:
        raise NotFoundError("Invoice not found")
    return inv


def apply_staged_document(inv: CommercialInvoice, data: InvoiceSave, *, tenant_schema: str | None = None):
    if not data.staged_file:
        return
    upload_dir = _upload_dir(tenant_schema)
    dest, orig = promote_staged(
        STAGE_SUBDIR, data.staged_file, inv.invoice_id,
        data.original_filename, ALLOWED_EXTENSIONS,
        perm_dir=upload_dir,
    )
    if dest:
        replace_document_path(inv, dest, orig)
        inv.file_pending = False
    if data.raw_extracted_data is not None:
        inv.raw_extracted_data = data.raw_extracted_data


def _sync_invoice_file_pending(inv: CommercialInvoice) -> None:
    inv.file_pending = not (inv.document_path and os.path.exists(inv.document_path))


def _resolve_invoice(db: Session, data: InvoiceSave, user_id: int) -> CommercialInvoice:
    if data.invoice_id:
        inv = db.query(CommercialInvoice).filter(
            CommercialInvoice.invoice_id == data.invoice_id).first()
        if not inv:
            raise NotFoundError("Invoice not found")
        return inv

    if data.shipment_id:
        inv = (db.query(CommercialInvoice)
               .filter(CommercialInvoice.shipment_id == data.shipment_id)
               .order_by(CommercialInvoice.invoice_id.desc()).first())
        if inv:
            return inv

    shipment = db.query(Shipment).filter(Shipment.shipment_id == data.shipment_id).first() \
        if data.shipment_id else None
    inv = CommercialInvoice(
        shipment_id=data.shipment_id,
        lc_id=shipment.lc_id if shipment else None,
        source="UPLOADED" if data.staged_file else "MANUAL",
        created_by=user_id,
    )
    db.add(inv)
    db.flush()
    return inv


def save_invoice(db: Session, data: InvoiceSave, user_id: int, *, tenant_schema: str | None = None) -> CommercialInvoice:
    inv = _resolve_invoice(db, data, user_id)

    overwrites = set(data.confirm_overwrites or [])
    if overwrites and inv.field_sources:
        for field in overwrites:
            if hasattr(data, field) and field in data.model_fields_set:
                setattr(inv, field, getattr(data, field))
                sources = dict(inv.field_sources or {})
                sources[field] = "EXTRACTED"
                inv.field_sources = sources

    apply_invoice_fields(inv, data, user_id)
    normalize_invoice_parties(inv, db)
    apply_staged_document(inv, data, tenant_schema=tenant_schema)
    _sync_invoice_file_pending(inv)
    if not data.staged_file and inv.source == "MANUAL":
        inv.file_pending = True
        sources = dict(inv.field_sources or {})
        for field in STR_FIELDS + DEC_FIELDS + ["total_coils"]:
            val = getattr(inv, field, None)
            if val not in (None, ""):
                sources[field] = sources.get(field) or "MANUAL"
        inv.field_sources = sources or None
    inv.status = "VERIFIED"
    db.flush()
    replace_line_items(inv, data.line_items, db)
    if inv.shipment_id:
        from modules.shipments.services import touch_docs_reception
        touch_docs_reception(inv.shipment_id, db)
    return inv


def update_invoice(db: Session, invoice_id: int, data: InvoiceSave, user_id: int) -> CommercialInvoice:
    inv = get_invoice_or_404(db, invoice_id)
    apply_invoice_fields(inv, data, user_id)
    normalize_invoice_parties(inv, db)
    db.flush()
    if "line_items" in data.model_fields_set:
        replace_line_items(inv, data.line_items, db)
    return inv


def delete_invoice(db: Session, invoice_id: int) -> CommercialInvoice:
    inv = get_invoice_or_404(db, invoice_id)
    if inv.document_path and os.path.exists(inv.document_path):
        try:
            os.remove(inv.document_path)
        except OSError:
            pass
    db.delete(inv)
    return inv
