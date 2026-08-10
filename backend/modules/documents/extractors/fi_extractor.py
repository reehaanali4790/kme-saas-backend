"""
Financial Instrument (FI) extractor using Claude Vision.
Targets the Pakistan Single Window (PSW) Portal "Financial Instrument / View Import"
print-out (label/value layout, usually 2 pages).
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

FI_PROMPT = """You are a document parser. Extract data from this Pakistan Single Window
(PSW) Portal FINANCIAL INSTRUMENT document (Financial Instrument / View Import).

The document is a label/value form, usually spanning 2 pages with sections:
"Basic Information", "Payment Information", "Item Information",
"Financial Transaction Information", "Declaration Information".
Read ALL pages. Find each field by its label; values appear directly beneath/after the label.

FIELDS:
fi_number          -> "Financial Instrument Unique No." (e.g. SBL-IMP-206533-20022026)
fi_status          -> "Status" under Basic Information (e.g. Active)
mode_of_payment    -> "Mode Of Payment" (e.g. Letter of Credit)
trader_ntn         -> "Trader NTN"
trader_name        -> "Trader Name" (the importer)
trader_iban        -> "Trader IBAN"
sight_pct          -> "Sight %" (number only)
beneficiary_name   -> "Beneficiary Name"
beneficiary_country-> "Beneficiary Country"
exporter_name      -> "Exporter Name"
exporter_country   -> "Exporter Country"
delivery_term      -> "Delivery Term" (e.g. Cost And Freight (CFR))
fi_value           -> "Financial Instrument Value" amount only (e.g. "USD 607,200" -> 607200)
fi_currency        -> currency of that value (e.g. USD)
exchange_rate      -> "Exchange Rate" (number)
lc_contract_no     -> "LC/Contract No."
balance            -> "Balance" amount only (number)
hs_code            -> "HS Code" from the Item Information table (e.g. 7210.4990)
goods_description  -> "Description" from the Item table
quantity           -> "Quantity" from the Item table (number only)
uom                -> "UOM" from the Item table (e.g. KG)
country_of_origin  -> "Country Of Origin" from the Item table
item_invoice_value -> "Item Invoice Value" amount only (number)
intended_payment_date  -> "Intended Payment Date"
final_date_of_shipment -> "Final Date of Shipment"
transport_document_date-> "Transport Document Date"
expiry_date        -> "Expiry Date" (THIS IS THE LAST DATE TO FILE THE GD)
declaration_number -> "Declaration Number" under Declaration Information (null if "No records available")

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "fi_number": null, "fi_status": null, "mode_of_payment": null, "trader_ntn": null,
  "trader_name": null, "trader_iban": null, "sight_pct": null,
  "beneficiary_name": null, "beneficiary_country": null, "exporter_name": null,
  "exporter_country": null, "delivery_term": null, "fi_value": null, "fi_currency": null,
  "exchange_rate": null, "lc_contract_no": null, "balance": null,
  "hs_code": null, "goods_description": null, "quantity": null, "uom": null,
  "country_of_origin": null, "item_invoice_value": null,
  "intended_payment_date": null, "final_date_of_shipment": null,
  "transport_document_date": null, "expiry_date": null, "declaration_number": null
}

Rules:
1. All dates -> YYYY-MM-DD. The portal prints DD-MM-YYYY, so "12-07-2026" -> "2026-07-12".
2. Money/amount fields -> number only, no currency word, no commas (e.g. "USD 607,200" -> 607200).
3. hs_code: copy exactly as printed (e.g. 7210.4990).
4. If Declaration Information shows "No records available", declaration_number is null.
5. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_fi(file_path: str, api_key: str) -> dict:
    """Extract Financial Instrument fields from an image or PDF via Claude Vision."""
    data = extract_with_tiers(file_path, FI_PROMPT, api_key, doc_type="fi")
    logger.info(f"FI extraction complete — fi_number={data.get('fi_number')}, "
                f"hs={data.get('hs_code')}, expiry={data.get('expiry_date')}")
    return data
