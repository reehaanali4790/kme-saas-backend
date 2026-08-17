"""Pydantic schemas for Financial Instruments (modules/documents/fi_router.py).

Field-update semantics from the original _apply(), preserved here:
  - STR_FIELDS: only applied when present with a non-None value (blank-after-strip
    becomes None); a key explicitly set to null is treated the same as an absent
    key - skipped, never clears an existing value. No model_fields_set needed since
    both cases collapse to the same no-op.
  - DEC_FIELDS: present-key-clears - if the key is present at all (even null/invalid),
    it's applied, clearing the field on a bad value. Needs model_fields_set.
  - DATE_FIELDS: only-if-truthy, silently skipped (not cleared) on an unparseable
    value. Same no-model_fields_set-needed logic as STR_FIELDS.
  - lc_id / shipment_id: present-key-clears with falsy-to-None ("data[f] or None").
  - status: only-if-truthy (a falsy/absent value is ignored, never clears status).

One deliberate, disclosed simplification: date parsing uses utils.parsing.parse_date,
which tolerates a full ISO datetime string ("2026-07-20T10:00:00") by truncating to
the date portion - the original's plain date.fromisoformat() would reject that input
and silently keep the prior value. Broader acceptance only, no valid input is now
rejected that wasn't before.
"""
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator

from utils.parsing import parse_date, parse_decimal

STR_FIELDS = ["fi_number", "fi_status", "mode_of_payment", "trader_ntn", "trader_name",
              "trader_iban", "beneficiary_name", "beneficiary_country", "exporter_name",
              "exporter_country", "delivery_term", "fi_currency", "lc_contract_no",
              "hs_code", "goods_description", "uom", "country_of_origin",
              "declaration_number", "notes"]
DATE_FIELDS = ["intended_payment_date", "final_date_of_shipment",
               "transport_document_date", "expiry_date"]
DEC_FIELDS = ["sight_pct", "fi_value", "exchange_rate", "balance",
              "quantity", "item_invoice_value"]


class FISave(BaseModel):
    fi_id: Optional[int] = None
    shipment_id: Optional[int] = None
    lc_id: Optional[int] = None
    status: Optional[str] = None
    staged_file: Optional[str] = None
    original_filename: Optional[str] = None
    raw_extracted_data: Optional[Dict[str, Any]] = None

    fi_number: Optional[str] = None
    fi_status: Optional[str] = None
    mode_of_payment: Optional[str] = None
    trader_ntn: Optional[str] = None
    trader_name: Optional[str] = None
    trader_iban: Optional[str] = None
    beneficiary_name: Optional[str] = None
    beneficiary_country: Optional[str] = None
    exporter_name: Optional[str] = None
    exporter_country: Optional[str] = None
    delivery_term: Optional[str] = None
    fi_currency: Optional[str] = None
    lc_contract_no: Optional[str] = None
    hs_code: Optional[str] = None
    goods_description: Optional[str] = None
    uom: Optional[str] = None
    country_of_origin: Optional[str] = None
    declaration_number: Optional[str] = None
    notes: Optional[str] = None

    intended_payment_date: Optional[date] = None
    final_date_of_shipment: Optional[date] = None
    transport_document_date: Optional[date] = None
    expiry_date: Optional[date] = None

    sight_pct: Optional[Decimal] = None
    fi_value: Optional[Decimal] = None
    exchange_rate: Optional[Decimal] = None
    balance: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    item_invoice_value: Optional[Decimal] = None

    @field_validator(*DATE_FIELDS, mode="before")
    @classmethod
    def _lenient_date(cls, v):
        return parse_date(v)

    @field_validator(*DEC_FIELDS, mode="before")
    @classmethod
    def _lenient_decimal(cls, v):
        return parse_decimal(v)
