"""Pydantic schemas for Commercial Invoices (modules/documents/invoice_router.py).

Field-update semantics from the original _apply_invoice_fields(), preserved here:
  - STR_FIELDS: only applied when present with a non-None value (never clears on an
    absent or explicit-null key) - same no-model_fields_set-needed logic as
    fi_schemas.py/insurance_schemas.py. Truncation to the DB column's max length
    (_cap) still happens in the service layer, not here.
  - DEC_FIELDS + total_coils: present-key-clears - needs model_fields_set.
  - invoice_date: only-if-truthy, silently skipped (not cleared) on a bad value.
  - There is no `status` field at all: the original _apply_invoice_fields() never
    reads a status key - POST/save_invoice always force-sets VERIFIED itself
    (service layer), and PUT/update_invoice never touches status.

line_items is intentionally a plain list of sub-schemas mirroring InvoiceLineItem's
columns 1:1 (all optional/lenient, since line items come from imperfect AI
extraction) rather than a rigid nested model with required fields.

One deliberate, disclosed simplification: invoice_date parsing uses
utils.parsing.parse_date, which additionally tolerates a full ISO datetime string by
truncating to the date portion - the original's plain date.fromisoformat() would
reject that input and silently keep the prior value. Broader acceptance only.
"""
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal

STR_FIELDS = ["invoice_number", "documentary_credit_number", "seller_name",
              "seller_address", "buyer_name", "buyer_address", "goods_description",
              "grade", "hs_code", "country_of_origin", "incoterms", "currency",
              "vessel_name", "voyage_number", "port_of_loading", "port_of_discharge"]
DEC_FIELDS = ["unit_price_usd", "total_net_weight_mt", "total_gross_weight_mt", "total_amount_usd"]


class InvoiceLineItemIn(BaseModel):
    item_number: Optional[int] = None
    size_thickness_mm: Optional[Decimal] = None
    size_width_mm: Optional[Decimal] = None
    quantity_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    gross_weight_mt: Optional[Decimal] = None
    number_of_coils: Optional[int] = None
    unit_price_usd: Optional[Decimal] = None
    line_amount_usd: Optional[Decimal] = None

    @field_validator("item_number", "number_of_coils", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        try:
            return int(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    @field_validator("size_thickness_mm", "size_width_mm", "quantity_mt", "net_weight_mt",
                     "gross_weight_mt", "unit_price_usd", "line_amount_usd", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class InvoiceSave(BaseModel):
    invoice_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    raw_extracted_data: Optional[Dict[str, Any]] = None

    invoice_number: Optional[str] = None
    documentary_credit_number: Optional[str] = None
    seller_name: Optional[str] = None
    seller_address: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_address: Optional[str] = None
    goods_description: Optional[str] = None
    grade: Optional[str] = None
    hs_code: Optional[str] = None
    country_of_origin: Optional[str] = None
    incoterms: Optional[str] = None
    currency: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_number: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None

    invoice_date: Optional[date] = None
    # Manual override for when this invoice was actually uploaded/received — present-key
    # clears (unlike invoice_date), same convention as the DEC_FIELDS group below.
    upload_date: Optional[date] = None

    unit_price_usd: Optional[Decimal] = None
    total_net_weight_mt: Optional[Decimal] = None
    total_gross_weight_mt: Optional[Decimal] = None
    total_amount_usd: Optional[Decimal] = None
    total_coils: Optional[int] = None

    line_items: Optional[List[InvoiceLineItemIn]] = None
    confirm_overwrites: Optional[List[str]] = None

    @field_validator("invoice_date", "upload_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator(*DEC_FIELDS, mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("total_coils", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        try:
            return int(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None
