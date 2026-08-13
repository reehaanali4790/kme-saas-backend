"""Pydantic schemas for LC creation (the Create-LC wizard's second step).
"""
from decimal import Decimal
from datetime import date
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator

from utils.parsing import extract_leading_number, parse_date


class LCCreate(BaseModel):
    contract_id: Optional[int] = None
    contract_number: Optional[str] = None
    lc_number: str
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    latest_shipment_date: Optional[date] = None
    applicant_name: Optional[str] = None
    beneficiary_name: Optional[str] = None
    currency: Optional[str] = None
    drafts_at: Optional[str] = None
    payment_terms: Optional[str] = None
    delivery_terms: Optional[str] = None
    issuing_bank: Optional[str] = None
    port_of_discharge: Optional[str] = None
    hoa: Optional[str] = None
    indentor: Optional[str] = None
    booked_by: Optional[str] = None
    insurance: Optional[str] = None
    insurance_company: Optional[str] = None
    reimbursement: Optional[str] = None
    exchange_rate: Optional[Decimal] = None
    raw_extracted_data: Optional[Any] = None
    country_of_origin: Optional[str] = None
    goods_description: Optional[str] = None
    quantity_mt: Optional[Decimal] = None
    unit_price_usd: Optional[Decimal] = None
    hs_code: Optional[str] = None
    amount: Optional[Decimal] = None
    buyer_allocation: Optional[dict] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    auto_create_shipment: bool = True

    @field_validator("lc_number", mode="before")
    @classmethod
    def _strip_lc_number(cls, v):
        return str(v or "").strip()

    @field_validator("lc_number")
    @classmethod
    def _lc_number_required(cls, v):
        if not v:
            raise ValueError("lc_number is required")
        return v

    @field_validator("issue_date", "expiry_date", "latest_shipment_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        """AI-extracted dates can be malformed; treat unparseable as absent rather
        than rejecting the whole request (matches the original _date() helper)."""
        return parse_date(v)

    @field_validator("exchange_rate", "quantity_mt", "unit_price_usd", "amount", mode="before")
    @classmethod
    def _extract_number(cls, v):
        """AI-extracted amounts can carry units/formatting, e.g. '593.00 PER M/TON'."""
        return extract_leading_number(v)


class LCCreateResult(BaseModel):
    success: bool = True
    lc_id: int
    lc_number: str
    warnings: List[str] = Field(default_factory=list)
    shipment_id: Optional[int] = None
