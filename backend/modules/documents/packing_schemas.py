"""Pydantic schemas for Packing Lists (modules/documents/packing_router.py).

Same field-update semantics as modules/documents/invoice_schemas.py (see that file's
docstring for the full breakdown): STR_FIELDS only-if-truthy/never-clears,
DEC_FIELDS + total_coils present-key-clears (needs model_fields_set), packing_date
only-if-truthy with silent-skip-on-failure, no `status` field (save_packing always
force-sets VERIFIED itself; update_packing never touches status).

Same disclosed simplification as elsewhere: packing_date uses utils.parsing.parse_date,
tolerating a full ISO datetime string where the original's plain date.fromisoformat()
would have rejected it and kept the prior value.
"""
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal

STR_FIELDS = ["packing_number", "goods_description", "hs_code"]
DEC_FIELDS = ["total_net_weight_mt", "total_gross_weight_mt"]


class PackingLineItemIn(BaseModel):
    item_number: Optional[int] = None
    grade: Optional[str] = None
    size: Optional[str] = None
    size_thickness_mm: Optional[Decimal] = None
    size_width_mm: Optional[Decimal] = None
    quantity_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    gross_weight_mt: Optional[Decimal] = None
    number_of_coils: Optional[int] = None

    @field_validator("item_number", "number_of_coils", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        try:
            return int(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    @field_validator("grade", "size", mode="before")
    @classmethod
    def _lenient_str(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("size_thickness_mm", "size_width_mm", "quantity_mt", "net_weight_mt",
                     "gross_weight_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class PackingSave(BaseModel):
    packing_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    raw_extracted_data: Optional[Dict[str, Any]] = None

    packing_number: Optional[str] = None
    goods_description: Optional[str] = None
    hs_code: Optional[str] = None

    packing_date: Optional[date] = None
    # Manual override for when this packing list was actually uploaded/received —
    # present-key clears (unlike packing_date), same convention as the DEC_FIELDS group below.
    upload_date: Optional[date] = None

    total_net_weight_mt: Optional[Decimal] = None
    total_gross_weight_mt: Optional[Decimal] = None
    total_coils: Optional[int] = None

    line_items: Optional[List[PackingLineItemIn]] = None

    @field_validator("packing_date", "upload_date", mode="before")
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
