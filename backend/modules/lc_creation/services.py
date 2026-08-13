"""LC creation business logic - contract cross-linking, party-name normalization,
column-width safety (fit_values), buyer allocation, and staged-document handoff.
"""
import logging
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session
from config.settings import settings
from core.exceptions import ValidationError
from models.database_models import BookedBy, Contract, Importer, Indentor, LCMaster, LCProduct, Supplier
from modules.lc_creation.helpers.buyer_allocation import apply_allocations
from modules.shipments.schemas import ShipmentCreate
from modules.shipments import services as shipment_svc
from infrastructure.field_limits.field_limits import FieldTooLong, fit_values
from infrastructure.normalization.normalization_service import (
    match_bank, match_company, normalize_item, clean_goods_description, validate_text_field, detect_quality,
)
from utils.uploads import safe_upload_path
from .schemas import LCCreate

logger = logging.getLogger("uvicorn")

UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "lc_documents")
STAGE_DIR = os.path.join(UPLOAD_DIR, "_staged")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def create_lc(db: Session, data: LCCreate, created_by: int) -> Tuple[LCMaster, List[str], Optional[int]]:
    contract = (
        db.query(Contract).filter(Contract.contract_id == data.contract_id).first()
        if data.contract_id else None
    )

    lc_date = data.issue_date or date.today()
    expiry = data.expiry_date
    monitoring = expiry or lc_date

    # Normalize party names to their clean canonical masters (overwrite raw with clean).
    raw_importer = data.applicant_name or (contract.buyer_name if contract else None)
    raw_supplier = data.beneficiary_name or (contract.supplier_name if contract else None)
    importer_name, _imp_row, _imp_rev = match_company(raw_importer, db, Importer)
    supplier_name, _sup_row, _sup_rev = match_company(raw_supplier, db, Supplier)
    raw_indentor = data.indentor or (contract.indentor_name if contract else None)
    indentor_name = None
    if raw_indentor:
        indentor_name, _ind_row, _ind_rev = match_company(raw_indentor, db, Indentor)
    booked_by_name = None
    if data.booked_by:
        booked_by_name, _bb_row, _bb_rev = match_company(data.booked_by, db, BookedBy)
    bank_name = None
    if data.issuing_bank:
        bank_name, _bank_row, _bank_rev = match_bank(data.issuing_bank, db)

    # payment_terms / delivery_terms / insurance are TEXT — full LC wording is stored as-is.
    # The rest are bounded; fit_values clamps + reports anything over its column width rather
    # than letting Postgres kill the INSERT with StringDataRightTruncation.
    lc_values = dict(
        lc_number=data.lc_number,
        contract_number=data.contract_number or (contract.contract_number if contract else None),
        contract_date=contract.contract_date if contract else None,
        supplier_name=supplier_name or raw_supplier,
        importer_name=importer_name or raw_importer,
        lc_date=lc_date, expiry_date=expiry,
        last_ship_date=data.latest_shipment_date,
        currency=data.currency,
        payment_terms=data.drafts_at or data.payment_terms,
        delivery_terms=data.delivery_terms,
        bank_name=bank_name or data.issuing_bank,
        arrival_port=data.port_of_discharge,
        monitoring_expiry=monitoring, status="OPEN",
        source="CONTRACT", contract_id=data.contract_id,
        # manual operational fields
        hoa=data.hoa,
        indentor=indentor_name or raw_indentor,
        booked_by=booked_by_name or data.booked_by,
        insurance=data.insurance or data.insurance_company,
        reimbursement=data.reimbursement,
        exchange_rate=data.exchange_rate,
        raw_extracted_data=data.raw_extracted_data,
        created_by=created_by,
    )
    try:
        # lc_number is the LC's identity — a clipped one would be WRONG, not just short,
        # so it's validated rather than clamped.
        lc_values, warnings = fit_values(LCMaster, lc_values, hard_fields={"lc_number"},
                                          labels={"lc_number": "LC Number"},
                                          context="LC create: ")
    except FieldTooLong as e:
        raise ValidationError(e.message)

    lc = LCMaster(**lc_values)
    db.add(lc)
    db.flush()

    origin = (data.country_of_origin or (contract.country_of_origin if contract else None) or "UNKNOWN")
    # A date-shaped origin (AI misread, or a stray value from the contract) is never a
    # valid country — same sanity check applied to the Excel importer, so this can't
    # resurface as "Country displayed as a Date" via the AI-extraction path either.
    origin, origin_review = validate_text_field(origin, "LC create: country_of_origin")
    if origin_review:
        origin = "UNKNOWN"
    # Normalize item type -> short code (closed set). Fall back to the HS/LC placeholder
    # for product_code so the NOT NULL column always has a value.
    item_code, item_review = normalize_item(data.goods_description)
    clean_product_name = clean_goods_description(data.goods_description)
    prod_values = dict(
        lc_id=lc.lc_id,
        product_code=(item_code or str(data.hs_code or "LC")),
        product_name=(clean_product_name or None),
        item_code=item_code, item_review=bool(item_review or origin_review),
        origin=str(origin), quality=detect_quality(data.goods_description),
        quantity=data.quantity_mt or Decimal("0"), unit="MT",
        lc_unit_price=data.unit_price_usd or Decimal("0"),
        hs_code=data.hs_code,
        lc_amount=data.amount,
    )
    # product_name is TEXT (full goods description); the rest are bounded and get clamped
    # + reported rather than blowing up the INSERT.
    prod_values, prod_warnings = fit_values(LCProduct, prod_values, context="LC create (product): ")
    warnings.extend(prod_warnings)
    db.add(LCProduct(**prod_values))

    # Structured buyer / booked-by allocation (optional; only when the form sends it).
    if data.buyer_allocation:
        lc_qty = data.quantity_mt or Decimal("0")
        lc_amount = data.amount
        if lc_amount is None and data.unit_price_usd is not None:
            lc_amount = lc_qty * data.unit_price_usd
        apply_allocations(db, lc, data.buyer_allocation, lc_qty, lc_amount)

    # move the staged LC document into place
    if data.staged_file:
        sp = os.path.join(STAGE_DIR, os.path.basename(data.staged_file))
        if os.path.exists(sp):
            orig = data.original_filename or ("lc" + Path(sp).suffix)
            dest = safe_upload_path(UPLOAD_DIR, lc.lc_id, orig, ALLOWED_EXTENSIONS)
            os.replace(sp, dest)
            lc.document_path = dest

    # contract now has an LC against it -> ACTUAL
    if contract:
        contract.status = "ACTUAL"

    try:
        db.commit()
    except DataError as e:
        # Belt-and-braces: a column we haven't widened still overflowed. Log exactly which
        # one server-side; the user gets a clean message, never the psycopg2 text.
        db.rollback()
        logger.error(f"LC create failed on a column limit (lc_number={data.lc_number}): {e.orig}",
                      exc_info=True)
        raise ValidationError(
            "One of the LC fields is longer than this system allows. "
            "Please shorten the longest field and try again.")
    db.refresh(lc)
    logger.info(f"LC created from contract: lc_id={lc.lc_id}, lc_number={lc.lc_number}, "
                f"contract={data.contract_id}")

    shipment_id: Optional[int] = None
    if data.auto_create_shipment:
        shipment, _bal = shipment_svc.create_shipment(
            ShipmentCreate(lc_id=lc.lc_id), db, created_by=created_by,
        )
        shipment_id = shipment.shipment_id
        logger.info(f"Auto-created shipment stub: shipment_id={shipment_id}, lc_id={lc.lc_id}")

    if warnings:
        logger.warning(f"LC {lc.lc_id} created with field warnings: {warnings}")
    return lc, warnings, shipment_id
