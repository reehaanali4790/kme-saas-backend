"""Pydantic schemas for the Bank Limit module.
"""
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

LC_TYPES = ("SIGHT", "DA", "BOTH")
BANK_LIMIT_TYPES = ("REGULAR", "ONE_OFF", "TEMPORARY")
BANK_LIMIT_TYPE_LABEL = {"REGULAR": "Regular", "ONE_OFF": "One-Off", "TEMPORARY": "Temporary"}

LcType = Literal["SIGHT", "DA", "BOTH"]
BankLimitTypeEnum = Literal["REGULAR", "ONE_OFF", "TEMPORARY"]


def _stripped_or_none(v) -> Optional[str]:
    s = (v or "").strip()
    return s or None


class BankLimitLineIn(BaseModel):
    company_name: str
    limit_type: str = "CHILD"
    lc_type: str = "BOTH"
    sub_limit_amount: Optional[float] = None

    @field_validator("company_name", mode="before")
    @classmethod
    def _strip_company_name(cls, v):
        return (v or "").strip()

    @model_validator(mode="after")
    def _sub_limit_not_negative(self) -> "BankLimitLineIn":
        if self.sub_limit_amount is not None and self.sub_limit_amount < 0:
            name = self.company_name or "line"
            raise ValueError(f"Sub-limit amount for {name} cannot be negative.")
        return self


class BankLimitCreate(BaseModel):
    bank_name: str
    branch: Optional[str] = None
    group_company: str
    bank_limit_type: BankLimitTypeEnum
    lc_type: LcType
    valid_from: date
    valid_to: date
    group_limit_amount: float = Field(..., ge=0)
    remarks: Optional[str] = None
    lines: List[BankLimitLineIn] = Field(default_factory=list)

    @field_validator("branch", "remarks", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return _stripped_or_none(v)

    @field_validator("bank_name", mode="before")
    @classmethod
    def _strip_bank_name(cls, v):
        return (v or "").strip()

    @field_validator("bank_name")
    @classmethod
    def _bank_name_required(cls, v):
        if not v:
            raise ValueError("Bank Name is required.")
        return v

    @field_validator("group_company", mode="before")
    @classmethod
    def _strip_group_company(cls, v):
        return (v or "").strip()

    @field_validator("group_company")
    @classmethod
    def _group_company_required(cls, v):
        if not v:
            raise ValueError("Group / Parent company is required.")
        return v

    @field_validator("valid_to")
    @classmethod
    def _valid_to_not_before_valid_from(cls, v, info):
        vf = info.data.get("valid_from")
        if vf and v < vf:
            raise ValueError("Valid To cannot be before Valid From.")
        return v


class BankLimitUpdate(BaseModel):
    bank_name: Optional[str] = None
    branch: Optional[str] = None
    group_company: Optional[str] = None
    bank_limit_type: Optional[BankLimitTypeEnum] = None
    lc_type: Optional[LcType] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    group_limit_amount: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = None
    lines: Optional[List[BankLimitLineIn]] = None

    @field_validator("branch", "remarks", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return _stripped_or_none(v)


class BankLimitLineOut(BaseModel):
    line_id: int
    company_name: str
    limit_type: str
    lc_type: str
    sub_limit_amount: Optional[float]


class BankLimitOut(BaseModel):
    limit_id: int
    bank_name: str
    branch: Optional[str]
    group_company: str
    bank_limit_type: str
    bank_limit_type_label: str
    lc_type: str
    valid_from: Optional[date]
    valid_to: Optional[date]
    group_limit_amount: Optional[float]
    revision_no: Optional[int]
    remarks: Optional[str]
    created_at: Optional[datetime]
    lines: List[BankLimitLineOut]


class BankLimitListOut(BaseModel):
    count: int
    items: List[BankLimitOut]


class BankLimitOptionsOut(BaseModel):
    banks: List[str]
    branches: List[str]
    groups: List[str]
    companies: List[str]


class SaveResult(BaseModel):
    success: bool = True
    limit_id: int
    revision_no: Optional[int] = None
    warnings: List[str] = Field(default_factory=list)
