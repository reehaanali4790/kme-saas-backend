"""
Commercial Invoice extractor using Claude Vision.
Extracts header fields + the line-item table (size breakdown).
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

INVOICE_PROMPT = """You are a trade-document parser. Extract data from this COMMERCIAL INVOICE
(a seller's invoice for an international steel shipment).

IMPORTANT — this file may be a COMBINED document containing several documents
(Commercial Invoice + Packing List + Bill of Lading), often on separate pages.
You are receiving ONLY the Commercial Invoice section (the relevant page(s) have
already been selected). Extract invoice data from these pages only — do NOT read
packing-list tables or bill-of-lading fields from other sections.

Find each field by searching for its label text anywhere on the document.
DO NOT assume a fixed position — labels vary between exporters. Read the WHOLE document,
including any line-item / goods table.

HEADER FIELDS (search by these label variations):

invoice_number       -> "Invoice No", "Invoice No.", "Commercial Invoice No", "INVOICE NO."
invoice_date         -> "Date", "Invoice Date", "Dated"
documentary_credit_number -> "Documentary Credit Number", "L/C No", "Letter of Credit No",
                            "Credit No", "DC No", "TF..." reference — this maps to the LC
seller_name          -> the company issuing the invoice (usually top header / "For and on behalf of")
seller_address       -> the seller's full address
buyer_name           -> labeled "To", "Buyer", "Consignee", "Messrs", "M/s"
buyer_address        -> the buyer's full address
goods_description    -> the description of goods (e.g. "PRIME HOT DIPPED GALVANIZED STEEL COILS")
grade                -> steel grade e.g. "DX-55D", "DX51D", "SGCC", "Grade ..."
hs_code              -> "HS Code", "H.S Code", "Harmonised Code", "HS Code No" (e.g. 7210.4990)
country_of_origin    -> "Country of Origin", "Origin", "...of China origin"
incoterms            -> "CFR", "CIF", "FOB", "FAS", "EXW", "Incoterms 2020", "CFR KARACHI PORT"
unit_price_usd       -> "The Rate", "Unit Price", "Rate", "USD/MT", "Price per MT" (the per-MT rate)
currency             -> "$", "USD", "EUR" etc.
vessel_name          -> "Ocean Vessel", "Vessel", "By Vessel", "Ocean Vessel: ..."
voyage_number        -> voyage code if present with the vessel
port_of_loading      -> "Port of Loading", "Shipment from", "From"
port_of_discharge    -> "Port of Discharge", "To ... Seaport", "to Karachi Seaport"
total_net_weight_mt  -> the TOTAL net weight in MT (from the totals row of the table)
total_gross_weight_mt-> the TOTAL gross weight in MT (totals row)
total_coils          -> the TOTAL number of coils/packages (totals row)
total_amount_usd     -> the grand total invoice value in USD ("Total", "Total Value", "Amount")

LINE ITEMS (the goods table — one object per row, NOT the totals row):
Each row typically has: item number, size (thickness x width in mm), quantity (MT),
net weight (MT), gross weight (MT), number of coils, unit rate (USD/MT), amount (USD).

For each line item extract:
  item_number        -> the row number (1, 2, 3, 4...)
  size_thickness_mm  -> thickness in mm from size like "1.0 X 1220" -> 1.0
  size_width_mm      -> width in mm from size like "1.0 X 1220" -> 1220
  quantity_mt        -> quantity in MT
  net_weight_mt      -> net weight in MT
  gross_weight_mt    -> gross weight in MT
  number_of_coils    -> coils in that row
  unit_price_usd     -> the per-MT rate for that row
  line_amount_usd    -> the amount for that row

Return ONLY a valid JSON object (use null for any field not found):
{
  "invoice_number": null, "invoice_date": null, "documentary_credit_number": null,
  "seller_name": null, "seller_address": null, "buyer_name": null, "buyer_address": null,
  "goods_description": null, "grade": null, "hs_code": null, "country_of_origin": null,
  "incoterms": null, "unit_price_usd": null, "currency": null,
  "vessel_name": null, "voyage_number": null, "port_of_loading": null, "port_of_discharge": null,
  "total_net_weight_mt": null, "total_gross_weight_mt": null, "total_coils": null,
  "total_amount_usd": null,
  "line_items": [
    {"item_number": 1, "size_thickness_mm": null, "size_width_mm": null, "quantity_mt": null,
     "net_weight_mt": null, "gross_weight_mt": null, "number_of_coils": null,
     "unit_price_usd": null, "line_amount_usd": null}
  ]
}

Non-negotiable rules:
1. All dates -> YYYY-MM-DD. Handle "30-Jan-26"->2026-01-30, "15/01/2026"->2026-01-15, "30-Jan-2026"->2026-01-30.
2. All weights/quantities -> decimal numbers ONLY, no units. If in KGS divide by 1000.
3. Counts (coils, total_coils) and item_number -> integers only.
4. Money (unit_price_usd, line_amount_usd, total_amount_usd) -> numbers only, no currency symbols/commas.
5. size like "1.0 X 1220" -> thickness 1.0, width 1220. "2.0X1220" same logic.
6. line_items: one object PER DATA ROW. Exclude the TOTAL row (its figures go in the total_* fields).
7. documentary_credit_number: copy EXACTLY as printed (it links to the LC).
8. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_invoice(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, INVOICE_PROMPT, api_key, doc_type="invoice")
    n = len(data.get("line_items") or [])
    logger.info(f"Invoice extraction complete — invoice_number={data.get('invoice_number')}, line_items={n}")
    return data
