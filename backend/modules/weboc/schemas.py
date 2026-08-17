"""Pydantic schemas for the WeBOC GD (Goods Declaration) module.
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from utils.parsing import parse_date, parse_decimal


class GDChargeIn(BaseModel):
    code: Optional[str] = None
    description: Optional[str] = None
    amount_pkr: Optional[float] = None

    @field_validator("code", "description", mode="before")
    @classmethod
    def _strip(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("amount_pkr", mode="before")
    @classmethod
    def _parse_float_lenient(cls, v):
        if v in (None, ""):
            return None
        try:
            return float(str(v).replace(",", "").strip())
        except (ValueError, TypeError):
            return None


class GDViewSave(BaseModel):
    gd_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    declaration_type: Optional[str] = None
    gd_type: Optional[str] = None
    gd_number: Optional[str] = None
    custom_office: Optional[str] = None
    importer_name: Optional[str] = None
    importer_ntn: Optional[str] = None
    exporter_name: Optional[str] = None
    vessel_name: Optional[str] = None
    bl_number: Optional[str] = None
    port_of_shipment: Optional[str] = None
    port_of_discharge: Optional[str] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    package_type: Optional[str] = None
    country_of_origin: Optional[str] = None

    filing_date: Optional[date] = None
    bl_date: Optional[date] = None
    eta: Optional[date] = None

    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    declared_value_pkr: Optional[Decimal] = None
    assessed_value_pkr: Optional[Decimal] = None
    declared_value_usd: Optional[Decimal] = None
    assessed_value_usd: Optional[Decimal] = None
    exchange_rate_pkr: Optional[Decimal] = None
    customs_duty_pkr: Optional[Decimal] = None
    sales_tax_pkr: Optional[Decimal] = None
    income_tax_pkr: Optional[Decimal] = None
    additional_customs_duty_pkr: Optional[Decimal] = None
    additional_sales_tax_pkr: Optional[Decimal] = None
    regulatory_duty_pkr: Optional[Decimal] = None
    igm_deblocking_pkr: Optional[Decimal] = None
    extra_pkr: Optional[Decimal] = None
    total_duties_pkr: Optional[Decimal] = None
    section82_penalty_pkr: Optional[Decimal] = None
    bonded_qty_mt: Optional[Decimal] = None

    package_count: Optional[int] = None
    charges_breakdown: Optional[List[GDChargeIn]] = None

    @field_validator(
        "gd_number", "custom_office", "importer_name", "importer_ntn", "exporter_name",
        "vessel_name", "bl_number", "port_of_shipment", "port_of_discharge",
        "hs_code", "goods_description", "package_type", "country_of_origin",
        "declaration_type", "gd_type",
        mode="before"
    )
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("filing_date", "bl_date", "eta", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator(
        "gross_weight_mt", "net_weight_mt", "declared_value_pkr", "assessed_value_pkr",
        "declared_value_usd", "assessed_value_usd", "exchange_rate_pkr",
        "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
        "additional_customs_duty_pkr", "additional_sales_tax_pkr",
        "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr", "total_duties_pkr",
        "section82_penalty_pkr", "bonded_qty_mt",
        mode="before"
    )
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("package_count", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


class GDItemIn(BaseModel):
    item_number: Optional[int] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    unit_type: Optional[str] = None
    declared_quantity: Optional[Decimal] = None
    assessed_quantity: Optional[Decimal] = None
    country_of_origin: Optional[str] = None
    sro_no: Optional[str] = None
    quota_reference: Optional[str] = None
    unit_value_declared: Optional[Decimal] = None
    unit_value_assessed: Optional[Decimal] = None
    total_value_declared_usd: Optional[Decimal] = None
    total_value_assessed_usd: Optional[Decimal] = None
    custom_value_declared_pkr: Optional[Decimal] = None
    custom_value_assessed_pkr: Optional[Decimal] = None

    @field_validator(
        "hs_code", "goods_description", "unit", "unit_type", "country_of_origin",
        "sro_no", "quota_reference",
        mode="before"
    )
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("item_number", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    @field_validator(
        "quantity", "declared_quantity", "assessed_quantity", "unit_value_declared",
        "unit_value_assessed", "total_value_declared_usd", "total_value_assessed_usd",
        "custom_value_declared_pkr", "custom_value_assessed_pkr",
        mode="before"
    )
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class ItemDetailsSave(BaseModel):
    gd_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    gd_number: Optional[str] = None
    importer_name: Optional[str] = None
    importer_ntn: Optional[str] = None
    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    hs_code: Optional[str] = None
    items: Optional[List[GDItemIn]] = None

    @field_validator("gd_number", "importer_name", "importer_ntn", "hs_code", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("gross_weight_mt", "net_weight_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class IntoBondGDSave(BaseModel):
    gd_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    gd_number: Optional[str] = None
    machine_number: Optional[str] = None
    custom_office: Optional[str] = None
    importer_name: Optional[str] = None
    importer_ntn: Optional[str] = None
    exporter_name: Optional[str] = None
    vessel_name: Optional[str] = None
    bl_number: Optional[str] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    package_type: Optional[str] = None
    girm_egm_number: Optional[str] = None

    filing_date: Optional[date] = None
    bl_date: Optional[date] = None
    assessment_date: Optional[date] = None

    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    bonded_qty_mt: Optional[Decimal] = None
    total_duties_pkr: Optional[Decimal] = None

    package_count: Optional[int] = None
    declaration_type: Optional[str] = None
    gd_type: Optional[str] = None

    @field_validator(
        "gd_number", "machine_number", "custom_office", "importer_name", "importer_ntn",
        "exporter_name", "vessel_name", "bl_number", "hs_code", "goods_description",
        "package_type", "girm_egm_number", "declaration_type", "gd_type",
        mode="before"
    )
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("filing_date", "bl_date", "assessment_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("gross_weight_mt", "net_weight_mt", "bonded_qty_mt", "total_duties_pkr", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("package_count", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


class ExBondGDSave(BaseModel):
    gd_id: Optional[int] = None
    shipment_id: Optional[int] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    gd_number: Optional[str] = None
    quantity_mt: Optional[Decimal] = None
    attachment_id: Optional[int] = None
    filing_date: Optional[date] = None
    ex_bond_date: Optional[date] = None
    duties_paid_pkr: Optional[Decimal] = None
    remarks: Optional[str] = None
    force: bool = False

    # Also map from extracted info in case some fields are passed from the document extraction
    customs_duty_pkr: Optional[Decimal] = None
    sales_tax_pkr: Optional[Decimal] = None
    income_tax_pkr: Optional[Decimal] = None
    additional_customs_duty_pkr: Optional[Decimal] = None
    additional_sales_tax_pkr: Optional[Decimal] = None
    regulatory_duty_pkr: Optional[Decimal] = None
    igm_deblocking_pkr: Optional[Decimal] = None
    extra_pkr: Optional[Decimal] = None
    total_duties_pkr: Optional[Decimal] = None
    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    declaration_type: Optional[str] = None
    gd_type: Optional[str] = None
    items: Optional[List[GDItemIn]] = None

    @field_validator("gd_number", "remarks", "declaration_type", "gd_type", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("filing_date", "ex_bond_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator(
        "quantity_mt", "duties_paid_pkr", "customs_duty_pkr", "sales_tax_pkr", "income_tax_pkr",
        "additional_customs_duty_pkr", "additional_sales_tax_pkr", "regulatory_duty_pkr",
        "igm_deblocking_pkr", "extra_pkr", "total_duties_pkr", "gross_weight_mt", "net_weight_mt",
        mode="before"
    )
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("attachment_id", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


class ExBondEntrySave(BaseModel):
    gd_number: Optional[str] = None
    ex_bond_date: Optional[date] = None
    quantity_mt: Optional[Decimal] = None
    remarks: Optional[str] = None
    duties_paid_pkr: Optional[Decimal] = None
    force: bool = False

    @field_validator("gd_number", "remarks", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("ex_bond_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("quantity_mt", "duties_paid_pkr", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class GdKgtlWeighmentSave(BaseModel):
    vehicle_number: Optional[str] = None
    weigh_date: Optional[date] = None
    own_weight_kg: Optional[Decimal] = None
    kgtl_weight_kg: Optional[Decimal] = None
    own_coils: Optional[int] = None
    kgtl_coils: Optional[int] = None
    remarks: Optional[str] = None

    @field_validator("own_coils", "kgtl_coils")
    @classmethod
    def _non_negative_coils(cls, v):
        if v is not None and v < 0:
            raise ValueError("coils must be zero or positive")
        return v

    @field_validator("vehicle_number", "remarks", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("weigh_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("own_weight_kg", "kgtl_weight_kg", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("own_weight_kg")
    @classmethod
    def _validate_own_weight(cls, v):
        if v is not None and v < 0:
            raise ValueError("own_weight_kg must be a positive weight in KG.")
        return v

    @field_validator("kgtl_weight_kg")
    @classmethod
    def _validate_kgtl_weight(cls, v):
        if v is not None and v < 0:
            raise ValueError("kgtl_weight_kg must be a positive weight in KG.")
        return v


class BondPenaltySave(BaseModel):
    penalty_pkr: Decimal
    source: str = "MANUAL"
    reason: Optional[str] = None
    force: bool = False

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, v):
        return str(v or "MANUAL").strip().upper()

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("penalty_pkr", mode="before")
    @classmethod
    def _parse_decimal_required(cls, v):
        dec = parse_decimal(v)
        if dec is None or dec < 0:
            raise ValueError("A penalty amount (PKR) is required.")
        return dec

    @model_validator(mode="after")
    def _validate_source_and_reason(self) -> "BondPenaltySave":
        if self.source not in ("DOCUMENT", "MANUAL"):
            raise ValueError("Source must be DOCUMENT or MANUAL.")
        if self.source == "MANUAL" and not self.reason:
            raise ValueError("A reason is required when entering a penalty manually.")
        return self


class PartialGdViewIn(BaseModel):
    """Extracted (or manually entered) EB GD View for one Partial GD."""
    gd_number: Optional[str] = None
    declaration_type: Optional[str] = None
    filing_date: Optional[date] = None
    ex_bond_date: Optional[date] = None
    quantity_mt: Optional[Decimal] = None
    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    remarks: Optional[str] = None

    @field_validator("gd_number", "declaration_type", "remarks", mode="before")
    @classmethod
    def _strip_str(cls, v):
        s = str(v).strip() if v not in (None, "") else ""
        return s or None

    @field_validator("filing_date", "ex_bond_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("quantity_mt", "gross_weight_mt", "net_weight_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class PartialGdItemDetailsIn(BaseModel):
    """Extracted (or manually entered) Item Details for one Partial GD document upload."""
    items: Optional[List[GDItemIn]] = None


class PartialGdPendingItemUpload(BaseModel):
    staged_file: str
    original_filename: Optional[str] = None
    items: Optional[List[GDItemIn]] = None


class PartialGdValidateApproval(BaseModel):
    approval_id: int
    quantity_mt: Optional[Decimal] = None
    force: bool = False
    staged_view_file: Optional[str] = None
    original_view_filename: Optional[str] = None
    view: Optional[PartialGdViewIn] = None
    pending_item_uploads: Optional[List["PartialGdPendingItemUpload"]] = None

    @field_validator("quantity_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)


class DeclarationTypeOverride(BaseModel):
    gd_type: str
    bonded_qty_mt: Optional[Decimal] = None

    @field_validator("gd_type", mode="before")
    @classmethod
    def _normalize_type(cls, v):
        return str(v or "").strip().upper()

    @field_validator("bonded_qty_mt", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @field_validator("gd_type")
    @classmethod
    def _validate_type(cls, v):
        if v not in ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND", "UNKNOWN"):
            raise ValueError("Invalid declaration type.")
        return v
