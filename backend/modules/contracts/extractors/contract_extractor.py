"""
Supplier Purchase / Sales Contract extractor using Claude Vision.
Reads the contract header + the goods line-item table into the Contract / ContractItem
shape. Codes (ERP internal) are not on the document, so they are not extracted; the
indentor and the actual purchase price are usually ERP-only and come back null (entered
manually). The L/C price/amount IS the contract price stated on the document.
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

CONTRACT_PROMPT = """You are a trade-document parser. Extract data from this SUPPLIER
PURCHASE / SALES CONTRACT — the commercial contract between an overseas supplier (seller)
and a Pakistani importer (buyer) that an LC is opened against.

STEP 1 — CLASSIFY THE DOCUMENT.
  document_type  -> the ACTUAL kind, e.g. "Sales Contract", "Purchase Contract",
                    "Proforma Invoice", "Commercial Invoice", "Bill of Lading",
                    "Goods Declaration", "Letter of Credit", or "Other".
  is_contract    -> true ONLY if this is a sales/purchase contract or a proforma invoice
                    that an LC would be opened against; false for anything else.
If is_contract is false, set every other field to null.

STEP 2 — if it IS a contract, extract the header and the goods line-item table.

HEADER:
contract_number       -> contract / sales-contract number (e.g. JLXL260327, FC2604-296-A)
contract_date         -> the contract date
valid_to              -> contract validity / expiry date if stated (else null)
supplier_name         -> the SELLER / supplier / beneficiary company
buyer_name            -> the BUYER / importer company
indentor_name         -> indentor / agent if named (usually absent -> null)
payment_terms         -> e.g. "LC at Sight 100%"
currency              -> e.g. USD
delivery_terms        -> Incoterm / delivery basis e.g. CFR, CFR Karachi, FOB
country_of_origin     -> goods origin country
buyer_ntn             -> buyer NTN if printed
buyer_address         -> buyer address
supplier_address      -> supplier address
quantity_tolerance    -> e.g. "+0%/-5%"
port_of_loading       -> load port
shipping_mark         -> shipping marks (N/M if none)
hs_code               -> HS code if stated
beneficiary_bank      -> beneficiary's bank name
beneficiary_swift     -> SWIFT code
beneficiary_account   -> beneficiary account number
beneficiary_bank_addr -> beneficiary bank address
documents_required    -> documents the seller must provide (comma-separated)
notes                 -> any other key note (else null)

LINE ITEMS — one object per product/size row in the goods table:
product_name      -> commodity / product name (e.g. "Prime Galvalume Steel Coils")
size_description  -> the size/spec text (e.g. "0.20-0.60MM x 900-1220MM")
grade_description -> steel grade if stated (e.g. DX-55D, SGCC) else null
mill_name         -> the producing mill / manufacturer name if stated (else null)
weight_mt         -> quantity in metric tons
lc_price          -> the contract UNIT price per MT — this is the L/C price
lc_amount         -> the line total amount (qty x price)
purchase_rate     -> a SEPARATE actual-purchase unit price if shown (usually absent -> null)
purchase_amount   -> separate purchase total if shown (usually null)

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "document_type": null, "is_contract": false,
  "contract_number": null, "contract_date": null, "valid_to": null,
  "supplier_name": null, "buyer_name": null, "indentor_name": null,
  "payment_terms": null, "currency": null, "delivery_terms": null, "country_of_origin": null,
  "buyer_ntn": null, "buyer_address": null, "supplier_address": null,
  "quantity_tolerance": null, "port_of_loading": null, "shipping_mark": null, "hs_code": null,
  "beneficiary_bank": null, "beneficiary_swift": null, "beneficiary_account": null,
  "beneficiary_bank_addr": null, "documents_required": null, "notes": null,
  "line_items": [
    {"product_name": null, "size_description": null, "grade_description": null, "mill_name": null,
     "weight_mt": null, "lc_price": null, "lc_amount": null,
     "purchase_rate": null, "purchase_amount": null}
  ]
}

Rules:
1. All dates -> YYYY-MM-DD.
2. Weights and money -> numbers only, no commas or currency symbols.
3. lc_price is the per-MT unit price; lc_amount is the line total.
4. A single-commodity contract still returns one object in line_items.
5. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_contract(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, CONTRACT_PROMPT, api_key, doc_type="contract")
    logger.info(f"Contract extraction complete — number={data.get('contract_number')}, "
                f"is_contract={data.get('is_contract')}, "
                f"items={len(data.get('line_items') or [])}")
    return data
