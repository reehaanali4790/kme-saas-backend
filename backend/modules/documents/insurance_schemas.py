"""Pydantic schemas for Insurance Certificates (modules/documents/insurance_router.py).

Same field-update semantics as modules/documents/fi_schemas.py (see that file's docstring
for the full breakdown): STR_FIELDS/DATE_FIELDS are only-if-truthy and never clear on a
falsy/absent value; DEC_FIELDS and lc_id/shipment_id are present-key-clears (needs
model_fields_set); status is only-if-truthy.

The original's DATE_FIELDS parsing already truncated to the date portion of an ISO
string (`str(data[f])[:10]`) before calling date.fromisoformat, so utils.parsing.parse_date
is an exact behavioral match here - no simplification needed, unlike fi_schemas.py.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal

STR_FIELDS = ["bl_number", "lc_number", "vessel_name", "certificate_number",
              "policy_number", "insurance_company", "currency", "voyage_route",
              "assured_name", "notes"]
DATE_FIELDS = ["issue_date"]
DEC_FIELDS = ["net_premium", "sum_insured", "gross_premium"]


class InsuranceSave(BaseModel):
    insurance_id: Optional[int] = None
    shipment_id: Optional[int] = None
    lc_id: Optional[int] = None
    status: Optional[str] = None

    bl_number: Optional[str] = None
    lc_number: Optional[str] = None
    vessel_name: Optional[str] = None
    certificate_number: Optional[str] = None
    policy_number: Optional[str] = None
    insurance_company: Optional[str] = None
    currency: Optional[str] = None
    voyage_route: Optional[str] = None
    assured_name: Optional[str] = None
    notes: Optional[str] = None

    issue_date: Optional[date] = None

    net_premium: Optional[Decimal] = None
    sum_insured: Optional[Decimal] = None
    gross_premium: Optional[Decimal] = None

    @field_validator(*DATE_FIELDS, mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator(*DEC_FIELDS, mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)
