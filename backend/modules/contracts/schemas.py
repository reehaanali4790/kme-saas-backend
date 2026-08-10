"""Pydantic schemas for the Contract module.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator

from utils.parsing import parse_date

STATUSES = ("DRAFT", "FINAL", "ACTUAL", "CANCELLED")


class ContractLineItemIn(BaseModel):
    line_no: Optional[int] = None
    product_name: Optional[str] = None
    size_description: Optional[str] = None
    grade_description: Optional[str] = None
    mill_name: Optional[str] = None
    weight_mt: Optional[Decimal] = None
    lc_price: Optional[Decimal] = None
    lc_amount: Optional[Decimal] = None
    purchase_rate: Optional[Decimal] = None
    purchase_amount: Optional[Decimal] = None

    @field_validator("product_name", "size_description", "grade_description", "mill_name", mode="before")
    @classmethod
    def _strip(cls, v):
        s = (str(v).strip() if v is not None else "")
        return s or None


class ContractSave(BaseModel):
    contract_id: Optional[int] = None
    lc_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    raw_extracted_data: Optional[Any] = None
    contract_number: Optional[str] = None
    supplier_name: Optional[str] = None
    buyer_name: Optional[str] = None
    indentor_name: Optional[str] = None
    payment_terms: Optional[str] = None
    currency: Optional[str] = None
    delivery_terms: Optional[str] = None
    country_of_origin: Optional[str] = None
    buyer_ntn: Optional[str] = None
    buyer_address: Optional[str] = None
    supplier_address: Optional[str] = None
    quantity_tolerance: Optional[str] = None
    port_of_loading: Optional[str] = None
    shipping_mark: Optional[str] = None
    hs_code: Optional[str] = None
    beneficiary_bank: Optional[str] = None
    beneficiary_swift: Optional[str] = None
    beneficiary_account: Optional[str] = None
    beneficiary_bank_addr: Optional[str] = None
    documents_required: Optional[str] = None
    notes: Optional[str] = None
    bank_name: Optional[str] = None
    contract_date: Optional[date] = None
    valid_to: Optional[date] = None
    status: Optional[str] = None
    line_items: Optional[List[ContractLineItemIn]] = None

    @field_validator("contract_date", "valid_to", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("status", mode="before")
    @classmethod
    def _lenient_status(cls, v):
        v = str(v or "").strip().upper()
        return v if v in STATUSES else None


class ContractStatusUpdate(BaseModel):
    status: str

    @field_validator("status", mode="before")
    @classmethod
    def _normalize(cls, v):
        return str(v or "").strip().upper()

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        if v not in STATUSES:
            raise ValueError(f"Invalid status. Allowed: {list(STATUSES)}")
        return v


class ContractLineItemOut(BaseModel):
    item_id: int
    line_no: Optional[int]
    item_code: Optional[str]
    item_review: bool
    product_name: Optional[str]
    size_description: Optional[str]
    grade_description: Optional[str]
    mill_name: Optional[str]
    weight_mt: Optional[float]
    lc_price: Optional[float]
    lc_amount: Optional[float]
    purchase_rate: Optional[float]
    purchase_amount: Optional[float]


class ContractOut(BaseModel):
    contract_id: int
    document_filename: Optional[str]
    has_document: bool
    source: Optional[str]
    status: Optional[str]
    lc_id: Optional[int]
    lc_number: Optional[str]
    sent_to_bank_at: Optional[datetime]
    lc_received_at: Optional[datetime]
    days_to_lc: Optional[int]
    contract_number: Optional[str]
    supplier_name: Optional[str]
    buyer_name: Optional[str]
    indentor_name: Optional[str]
    payment_terms: Optional[str]
    currency: Optional[str]
    delivery_terms: Optional[str]
    country_of_origin: Optional[str]
    buyer_ntn: Optional[str]
    buyer_address: Optional[str]
    supplier_address: Optional[str]
    quantity_tolerance: Optional[str]
    port_of_loading: Optional[str]
    shipping_mark: Optional[str]
    hs_code: Optional[str]
    beneficiary_bank: Optional[str]
    beneficiary_swift: Optional[str]
    beneficiary_account: Optional[str]
    beneficiary_bank_addr: Optional[str]
    documents_required: Optional[str]
    notes: Optional[str]
    bank_name: Optional[str]
    contract_date: Optional[date]
    valid_to: Optional[date]
    line_items: List[ContractLineItemOut]
    total_weight_mt: Optional[float]
    total_lc_amount: Optional[float]
    total_purchase_amount: Optional[float]


class BankPerformanceRow(BaseModel):
    """A bank's historical LC issuance turnaround, averaged across every completed
    contract (both sent_to_bank_at and a linked LC's import_date present)."""
    bank: str
    contract_count: int
    avg_days_to_lc: float
    min_days_to_lc: int
    max_days_to_lc: int


class SaveResult(BaseModel):
    success: bool = True
    contract_id: int


class StatusResult(BaseModel):
    success: bool = True
    contract_id: int
    status: str
