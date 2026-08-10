"""Pydantic schemas for SRO / EDB Quota management (modules/weboc/sro_router.py).

Field-update semantics here are UNIFORM (unlike gd_schemas.py's mixed rules): every
field on ApprovalSave and SroQuotaItemSave applies whenever the key is PRESENT in the
payload, even if that clears the field to None (matches the original _apply_approval()/
update_item(), both of which use `if f in data: setattr(...)` for every field without
exception). See modules/weboc/sro_service.py's apply_approval_fields()/apply_item_fields().
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal


def _strip_or_none(v) -> Optional[str]:
    s = (str(v).strip() if v is not None else "")
    return s or None


class SroGroupNumberIn(BaseModel):
    group_sro_no: Optional[str] = None
    hs_code: Optional[str] = None

    @field_validator("group_sro_no", "hs_code", mode="before")
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)


class ApprovalSave(BaseModel):
    approval_no: Optional[str] = None
    company_name: Optional[str] = None
    main_sro_no: Optional[str] = None
    sro_no: Optional[str] = None
    sro_number: Optional[str] = None
    hs_code: Optional[str] = None
    part_name: Optional[str] = None
    use_type: Optional[str] = None
    unit: Optional[str] = None
    short_description: Optional[str] = None
    notes: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    approved_qty_mt: Optional[Decimal] = None
    group_sro_numbers: Optional[List[SroGroupNumberIn]] = None

    @field_validator(
        "approval_no", "company_name", "main_sro_no", "sro_no", "sro_number", "hs_code", "part_name", "use_type",
        "unit", "short_description", "notes",
        mode="before",
    )
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("approved_qty_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("group_sro_numbers", mode="before")
    @classmethod
    def _lenient_list(cls, v):
        return v if isinstance(v, list) else None


class SroQuotaItemSave(BaseModel):
    approval_id: Optional[int] = None
    hs_code: Optional[str] = None
    part_name: Optional[str] = None
    declared_qty_mt: Optional[Decimal] = None
    assessed_qty_mt: Optional[Decimal] = None

    @field_validator("hs_code", "part_name", mode="before")
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)

    @field_validator("declared_qty_mt", "assessed_qty_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)
