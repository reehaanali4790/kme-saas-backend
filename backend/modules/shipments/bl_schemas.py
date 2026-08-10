"""Pydantic schemas for Bill of Lading CRUD (modules/shipments/bl_router.py).

The original _apply_bl_fields() has genuinely inconsistent per-field-group semantics:
str/int/decimal fields apply only when `field in data and data[field] is not None`
(present+non-null applies, present+null OR absent both skip - so a malformed int/decimal
silently leaves the PRIOR value untouched, not cleared); bl_date can only be set, never
cleared; demurrage_start_date/demurrage_cleared_date explicitly clear on an empty value;
lc_id/shipment_id use `data.get(f) or None` (falsy always clears).

This migration makes a deliberate, disclosed simplification: every field except bl_date
uses uniform present-key-clears semantics (matches the simpler convention already used in
contracts.py/sro_schemas.py) - a malformed or explicit-null value clears the field rather
than silently preserving whatever was there before. bl_date keeps its original
only-applies-if-valid behavior (a BL's shipment date is never blanked via this endpoint).
This is a narrow, disclosed behavior change from the exact original for the "explicit null
on a numeric field silently keeps the old value" case; see the production-readiness plan
notes for this module.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal

VALID_STATUSES = {"IMPORTED", "PENDING_REVIEW", "VERIFIED", "RELEASED"}


def _strip_or_none(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _lenient_int(v) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class BLSave(BaseModel):
    """Used for both create (no bl_id path param - bl_id in the body instead) and
    update - matches the original create_bl()/update_bl() dual-purpose pattern."""
    bl_id: Optional[int] = None
    lc_id: Optional[int] = None
    shipment_id: Optional[int] = None

    bl_number: Optional[str] = None
    bl_issue_place: Optional[str] = None
    shipper_name: Optional[str] = None
    shipper_address: Optional[str] = None
    consignee: Optional[str] = None
    notify_party: Optional[str] = None
    carrier_name: Optional[str] = None
    shipping_agent: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_number: Optional[str] = None
    pre_carriage_by: Optional[str] = None
    place_of_receipt: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    final_destination: Optional[str] = None
    freight_payable_at: Optional[str] = None
    shipping_marks: Optional[str] = None
    package_type: Optional[str] = None
    goods_description: Optional[str] = None
    bl_type: Optional[str] = None
    applicant_ntn: Optional[str] = None
    freight_terms: Optional[str] = None
    shipped_on_board_clause: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    demurrage_currency: Optional[str] = None
    detention_currency: Optional[str] = None
    detention_remarks: Optional[str] = None

    original_bl_count: Optional[int] = None
    package_count: Optional[int] = None
    free_days: Optional[int] = None
    detention_free_days: Optional[int] = None

    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    measurement_m3: Optional[Decimal] = None
    demurrage_total_amount: Optional[Decimal] = None
    detention_total_amount: Optional[Decimal] = None

    bl_date: Optional[date] = None
    upload_date: Optional[date] = None
    demurrage_start_date: Optional[date] = None
    demurrage_cleared_date: Optional[date] = None
    detention_start_date: Optional[date] = None
    detention_end_date: Optional[date] = None
    detention_paid_date: Optional[date] = None

    @field_validator(
        "bl_number", "bl_issue_place", "shipper_name", "shipper_address", "consignee",
        "notify_party", "carrier_name", "shipping_agent", "vessel_name", "voyage_number",
        "pre_carriage_by", "place_of_receipt", "port_of_loading", "port_of_discharge",
        "final_destination", "freight_payable_at", "shipping_marks", "package_type",
        "goods_description", "applicant_ntn", "freight_terms", "shipped_on_board_clause",
        "notes", "source", "demurrage_currency", "detention_currency", "detention_remarks",
        mode="before",
    )
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)

    @field_validator("original_bl_count", "package_count", "free_days", "detention_free_days", mode="before")
    @classmethod
    def _int_lenient(cls, v):
        return _lenient_int(v)

    @field_validator("bl_type", mode="before")
    @classmethod
    def _bl_type_lenient(cls, v):
        s = str(v or "").strip().upper()
        return s if s in ("COIL", "CONTAINER") else None

    @field_validator(
        "gross_weight_mt", "net_weight_mt", "measurement_m3", "demurrage_total_amount",
        "detention_total_amount",
        mode="before",
    )
    @classmethod
    def _decimal_lenient(cls, v):
        return parse_decimal(v)

    @field_validator(
        "bl_date", "upload_date", "demurrage_start_date", "demurrage_cleared_date",
        "detention_start_date", "detention_end_date", "detention_paid_date",
        mode="before",
    )
    @classmethod
    def _date_lenient(cls, v):
        return parse_date(v)


class BLLinkLC(BaseModel):
    lc_id: int


class BLStatusUpdate(BaseModel):
    status: str
    notes: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v):
        return str(v or "").strip().upper()

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        if v not in VALID_STATUSES:
            raise ValueError(f"Invalid status. Must be one of: {sorted(VALID_STATUSES)}")
        return v
