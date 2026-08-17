"""Pydantic schemas for the Shipment hub (modules/shipments/router.py) - write-side
only (create/update). The summary/detail read views aggregate 6+ related document
types into a large, actively-evolving nested structure (see services.py's
shipment_summary()/shipment_detail()) - kept as plain dicts rather than a rigid
response schema, same reasoning as schemas/bank_limits.py's /report endpoint.
"""
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from utils.parsing import parse_date, parse_decimal

# Actual-event dates cannot be in the future (they describe things that already
# happened). ETA + Maturity Date are estimates/forward-dated, so they stay open.
_FUTURE_BLOCKED_LABELS = {
    "bl_date": "BL Date", "etd": "ETD",
    "intimation_date": "Intimation Date", "payment_date": "Payment Date",
    "original_doc_date": "Original Document Date", "delivery_date": "Delivery Date",
}


def _strip_or_none(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


class ShipmentCreate(BaseModel):
    contract_id: int
    import_mode: Literal["LC_BACKED", "NON_LC", "TT", "CAD"] = "LC_BACKED"
    lc_id: Optional[int] = None
    lc_waiver_reason: Optional[str] = None
    category: Optional[str] = None
    lot_number: Optional[str] = None
    shipment_ref: Optional[str] = None
    override_reason: Optional[str] = None

    @field_validator("category", "lot_number", "shipment_ref", "lc_waiver_reason", mode="before")
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)

    @model_validator(mode="after")
    def _validate_import_path(self) -> "ShipmentCreate":
        if self.import_mode == "LC_BACKED":
            if not self.lc_id:
                raise ValueError("lc_id is required for LC-backed imports")
        else:
            if not (self.lc_waiver_reason or "").strip():
                raise ValueError("lc_waiver_reason is required when import_mode is not LC_BACKED")
        return self


class ShipmentUpdate(BaseModel):
    """Field-update semantics are NOT uniform (matches the original update_shipment()):
      - free-text fields: present-key clears (see apply_shipment_fields())
      - bl_date/etd: only applied if truthy (absent/falsy leaves the column alone)
      - milestone dates + exchange_rate: present-key clears, same as free-text
      - eta: a truthy value locks it (eta_source='MANUAL'); an explicit null clears it back
        to the auto-formula. transit_days present-key-clears and (like a truthy etd) triggers
        an auto-formula recalculation while eta_source is still 'AUTO'/unset. eta_source is a
        write-only trigger — send "AUTO" to explicitly reset a locked ETA back to the formula.
        See apply_shipment_fields() for the full precedence rules.
    status is intentionally NOT a field here - it's derived automatically from
    milestone dates + documents (see services.recompute_shipment_status), never
    user-settable directly.
    """
    shipment_ref: Optional[str] = None
    category: Optional[str] = None
    lot_number: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_number: Optional[str] = None
    port_of_loading: Optional[str] = None
    port_of_discharge: Optional[str] = None
    vessel_location: Optional[str] = None
    validation_notes: Optional[str] = None
    remarks: Optional[str] = None

    bl_date: Optional[date] = None
    eta: Optional[date] = None
    etd: Optional[date] = None

    payment_date: Optional[date] = None
    original_doc_date: Optional[date] = None
    intimation_date: Optional[date] = None
    maturity_date: Optional[date] = None
    delivery_date: Optional[date] = None
    retirement_date: Optional[date] = None

    # Manual vessel port-status override — kpt_berth is a plain field (present-key clears,
    # same as the other free-text fields); port_status is a write-only trigger that, when
    # truthy, normalizes vessel_location/kpt_berth/on_port_date/departure_date together via
    # apply_port_status() instead of being stored verbatim (see apply_shipment_fields()).
    kpt_berth: Optional[str] = None
    port_status: Optional[str] = None
    on_port_date: Optional[date] = None
    departure_date: Optional[date] = None

    # Auto-ETA estimation (etd + transit_days business days) — see Shipment.eta_source.
    transit_days: Optional[int] = None
    eta_source: Optional[str] = None

    exchange_rate: Optional[Decimal] = None
    override_reason: Optional[str] = None

    @field_validator(
        "shipment_ref", "category", "lot_number", "vessel_name", "voyage_number",
        "port_of_loading", "port_of_discharge", "vessel_location", "validation_notes",
        "remarks", "kpt_berth", "port_status", "eta_source",
        mode="before",
    )
    @classmethod
    def _strip(cls, v):
        return _strip_or_none(v)

    @field_validator("transit_days", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    @field_validator(
        "bl_date", "eta", "etd", "payment_date", "original_doc_date", "intimation_date",
        "maturity_date", "delivery_date", "retirement_date", "on_port_date", "departure_date",
        mode="before",
    )
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator("exchange_rate", mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)

    @model_validator(mode="after")
    def _no_future_event_dates(self) -> "ShipmentUpdate":
        today = date.today()
        bad = [label for field, label in _FUTURE_BLOCKED_LABELS.items()
               if getattr(self, field) and getattr(self, field) > today]
        if bad:
            raise ValueError(f"These dates cannot be in the future: {', '.join(bad)}.")
        return self


class ShipmentCreateResult(BaseModel):
    success: bool = True
    shipment_id: int
    category: Optional[str] = None
    lc_balance: dict = Field(default_factory=dict)
