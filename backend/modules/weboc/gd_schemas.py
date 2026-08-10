"""Pydantic schemas for the base Goods Declaration CRUD (modules/weboc/gd_router.py).

Distinct from modules/weboc/schemas.py, which covers the GD-view/item-details/
into-bond/ex-bond "extended workflow" cluster (modules/weboc/router.py) - this file
is for the foundational GD record itself: create/save, item lines, status.

Field-level update semantics are intentionally preserved exactly from the original
_apply()/_to_dict() in gd_router.py, which is NOT uniform across field groups:
  - string/date fields: only applied if truthy (falsy/absent leaves the column alone)
  - decimal fields: applied whenever the key is PRESENT, even if it parses to None
    (an explicit null/empty string actively CLEARS the column - these are real money
    figures on customs duty reports, so this distinction matters)
  - bool fields: applied only if present AND not None
See modules/weboc/gd_service.py's apply_gd_fields() for where each rule is enforced.
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal, parse_formatted_number

GD_TYPES = ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND")


class LevyIn(BaseModel):
    code: Optional[str] = None
    rate: Optional[float] = None
    amount_pkr: Optional[float] = None

    @field_validator("code", mode="before")
    @classmethod
    def _strip_code(cls, v):
        s = (str(v).strip() if v is not None else "")
        return s or None

    @field_validator("rate", "amount_pkr", mode="before")
    @classmethod
    def _lenient_float(cls, v):
        """Matches the original _num() helper's silent-fallback-to-None."""
        return parse_formatted_number(v)


class GDItemIn(BaseModel):
    item_number: Optional[int] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit_type: Optional[str] = None
    country_of_origin: Optional[str] = None
    sro_no: Optional[str] = None
    quota_reference: Optional[str] = None
    m_size: Optional[str] = None
    unit_value_declared: Optional[Decimal] = None
    unit_value_assessed: Optional[Decimal] = None
    total_value_declared_usd: Optional[Decimal] = None
    total_value_assessed_usd: Optional[Decimal] = None
    custom_value_declared_pkr: Optional[Decimal] = None
    custom_value_assessed_pkr: Optional[Decimal] = None
    levies: Optional[List[LevyIn]] = None

    @field_validator("hs_code", "goods_description", "unit_type", "country_of_origin", "sro_no", "quota_reference", "m_size",
                      mode="before")
    @classmethod
    def _strip(cls, v):
        s = (str(v).strip() if v is not None else "")
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
        "quantity", "unit_value_declared", "unit_value_assessed", "total_value_declared_usd",
        "total_value_assessed_usd", "custom_value_declared_pkr", "custom_value_assessed_pkr",
        mode="before",
    )
    @classmethod
    def _lenient_decimal(cls, v):
        """AI-extracted item values can be malformed/non-numeric; matches the original
        _dec() helper's silent-fallback-to-None rather than rejecting the request."""
        return parse_decimal(v)

    @field_validator("levies", mode="before")
    @classmethod
    def _lenient_levies(cls, v):
        """Matches the original _clean_levies()'s `if not isinstance(levies, list): return
        None` guard - AI extraction can hand back a non-list value here."""
        return v if isinstance(v, list) else None


class GDSave(BaseModel):
    """Used for both create (no gd_id) and update - save_gd serves both, matching
    the original dual-purpose endpoint (see schemas/contracts.py's ContractSave for
    the same pattern)."""
    gd_id: Optional[int] = None
    shipment_id: Optional[int] = None

    gd_number: Optional[str] = None
    machine_number: Optional[str] = None
    custom_office: Optional[str] = None
    bank_code: Optional[str] = None
    girm_egm_number: Optional[str] = None
    financial_instrument_no: Optional[str] = None
    importer_ntn: Optional[str] = None
    importer_name: Optional[str] = None
    exporter_name: Optional[str] = None
    vessel_name: Optional[str] = None
    bl_number: Optional[str] = None
    port_of_shipment: Optional[str] = None
    port_of_discharge: Optional[str] = None
    payment_terms: Optional[str] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    package_type: Optional[str] = None
    country_of_origin: Optional[str] = None
    notes: Optional[str] = None
    examination_report: Optional[str] = None
    vir_no: Optional[str] = None
    index_no: Optional[str] = None
    lab_officer: Optional[str] = None
    lab_report: Optional[str] = None
    assessment_report: Optional[str] = None

    filing_date: Optional[date] = None
    assessment_date: Optional[date] = None
    financial_instrument_date: Optional[date] = None
    bl_date: Optional[date] = None
    examined_date: Optional[date] = None

    lab_report_available: Optional[bool] = None
    lab_report_assessment: Optional[bool] = None

    financial_instrument_value: Optional[Decimal] = None
    gross_weight_mt: Optional[Decimal] = None
    net_weight_mt: Optional[Decimal] = None
    bonded_qty_mt: Optional[Decimal] = None
    declared_unit_value_usd: Optional[Decimal] = None
    assessed_unit_value_usd: Optional[Decimal] = None
    declared_value_usd: Optional[Decimal] = None
    assessed_value_usd: Optional[Decimal] = None
    declared_value_pkr: Optional[Decimal] = None
    assessed_value_pkr: Optional[Decimal] = None
    exchange_rate_pkr: Optional[Decimal] = None
    freight_value_usd: Optional[Decimal] = None
    insurance_value_usd: Optional[Decimal] = None
    landing_charges_pct: Optional[Decimal] = None
    customs_duty_pkr: Optional[Decimal] = None
    sales_tax_pkr: Optional[Decimal] = None
    income_tax_pkr: Optional[Decimal] = None
    additional_customs_duty_pkr: Optional[Decimal] = None
    additional_sales_tax_pkr: Optional[Decimal] = None
    regulatory_duty_pkr: Optional[Decimal] = None
    igm_deblocking_pkr: Optional[Decimal] = None
    extra_pkr: Optional[Decimal] = None
    invoice_not_found_penalty_pkr: Optional[Decimal] = None
    total_duties_pkr: Optional[Decimal] = None

    package_count: Optional[int] = None

    gd_type: Optional[str] = None
    declaration_type: Optional[str] = None  # raw Field 2 value (e.g. "IB"), resolved in the service
    levy_breakdown: Optional[List[LevyIn]] = None
    items: Optional[List[GDItemIn]] = None
    final_gd: Optional[bool] = None
    status: Optional[str] = None

    @field_validator("levy_breakdown", "items", mode="before")
    @classmethod
    def _lenient_list(cls, v):
        """Matches the original's `if not isinstance(levies, list)` / iteration-over-items
        guards - AI extraction can hand back a non-list value for either of these."""
        return v if isinstance(v, list) else None

    @field_validator(
        "gd_number", "machine_number", "custom_office", "bank_code", "girm_egm_number",
        "financial_instrument_no", "importer_ntn", "importer_name", "exporter_name",
        "vessel_name", "bl_number", "port_of_shipment", "port_of_discharge", "payment_terms",
        "hs_code", "goods_description", "package_type", "country_of_origin", "notes",
        "examination_report", "vir_no", "index_no", "lab_officer", "lab_report",
        "assessment_report",
        mode="before",
    )
    @classmethod
    def _strip_str_fields(cls, v):
        s = (str(v).strip() if v is not None else "")
        return s or None

    @field_validator(
        "filing_date", "assessment_date", "financial_instrument_date", "bl_date",
        "examined_date",
        mode="before",
    )
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("gd_type")
    @classmethod
    def _valid_gd_type_or_none(cls, v):
        return v if v in GD_TYPES else None

    @field_validator("package_count", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    @field_validator(
        "financial_instrument_value", "gross_weight_mt", "net_weight_mt", "bonded_qty_mt",
        "declared_unit_value_usd", "assessed_unit_value_usd", "declared_value_usd",
        "assessed_value_usd", "declared_value_pkr", "assessed_value_pkr", "exchange_rate_pkr",
        "freight_value_usd", "insurance_value_usd", "landing_charges_pct", "customs_duty_pkr",
        "sales_tax_pkr", "income_tax_pkr", "additional_customs_duty_pkr",
        "additional_sales_tax_pkr", "regulatory_duty_pkr", "igm_deblocking_pkr", "extra_pkr",
        "invoice_not_found_penalty_pkr", "total_duties_pkr",
        mode="before",
    )
    @classmethod
    def _lenient_decimal(cls, v):
        """AI-extracted/legacy values can be malformed/non-numeric; matches the original
        _dec() helper's silent-fallback-to-None rather than rejecting the request. Note
        this is still applied whenever the key is present (see apply_gd_fields) - a
        malformed value becomes an explicit clear (None), same as the original."""
        return parse_decimal(v)


class GDStatusUpdate(BaseModel):
    status: str

    @field_validator("status", mode="before")
    @classmethod
    def _normalize(cls, v):
        return str(v or "").strip().upper()


class GDSaveResult(BaseModel):
    success: bool = True
    gd_id: int
    status: Optional[str] = None


class GDAttachmentOut(BaseModel):
    attachment_id: int
    gd_id: int
    kind: str
    filename: Optional[str]
    uploaded_at: Optional[str]
