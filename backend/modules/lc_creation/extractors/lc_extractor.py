"""
Letter of Credit (SWIFT MT700) extractor using Claude Vision.
Reads the issued documentary credit into the fields used to create an LCMaster +
LCProduct. A SWIFT LC PDF can be 100+ pages (the original 700 plus amendments and
repeats); we cap the pages sent — the user uploads only the LC detail pages anyway.
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

# Only the first detail pages carry the fin.700 message; cap to keep the request sane.
MAX_LC_PAGES = 8

LC_PROMPT = """You are a trade-finance parser. Extract data from this SWIFT MT700
"Issue of a Documentary Credit" (Letter of Credit). Fields are tagged with SWIFT codes
(F20, F31C, F32B, ...). Read them from anywhere on the document.

STEP 1 — CLASSIFY:
  document_type -> e.g. "Letter of Credit (MT700)", "Documentary Credit", "Sales Contract",
                   "Commercial Invoice", "Bill of Lading", or "Other".
  is_lc         -> true if this is a Letter of Credit / Documentary Credit in ANY form —
                   a raw SWIFT MT700 (with F-codes like F20, F31C) OR a bank-issued /
                   formatted documentary credit WITHOUT SWIFT codes. Signals of an LC:
                   a documentary-credit / LC number, the word IRREVOCABLE, an applicant
                   AND a beneficiary, an expiry date, and a "documents required" clause.
                   Set false ONLY for a clearly different stand-alone document (a commercial
                   invoice, packing list, or bill of lading by itself, a GD, or a contract).
The SWIFT F-codes below are hints for where fields are; a formatted LC will have the same
information under plain labels — map by meaning, not just by code.
If is_lc is false, set every other field to null.

STEP 2 — extract:
lc_number            -> F20 Documentary Credit Number (e.g. 1089LC63571/2026)
issue_date           -> F31C Date of Issue
form                 -> F40A Form (e.g. IRREVOCABLE)
applicable_rules     -> F40E (e.g. UCP LATEST VERSION)
expiry_date          -> F31D Date of Expiry (date part)
expiry_place         -> F31D place (e.g. AT CHINA)
applicant_name       -> F50 Applicant (the importer/buyer company)
beneficiary_name     -> F59 Beneficiary (the supplier/seller company)
currency             -> F32B currency (e.g. USD)
amount               -> F32B amount (number only)
available_with       -> F41D Available With ... By ... (e.g. ANY BANK IN CHINA BY NEGOTIATION)
drafts_at            -> F42C Drafts at (e.g. SIGHT)
drawee_bank          -> F42D Drawee bank
issuing_bank         -> the Sender / issuing bank (e.g. BANK AL HABIB)
advising_bank        -> the Receiver / advising bank
partial_shipments    -> F43P (ALLOWED / NOT ALLOWED)
transhipment         -> F43T (ALLOWED / NOT ALLOWED)
port_of_loading      -> F44E Port of Loading
port_of_discharge    -> F44F Port of Discharge
latest_shipment_date -> F44C Latest Date of Shipment
goods_description     -> F45A description of goods — the COMMODITY NAME ONLY
                         (e.g. "Cold Rolled Coils Secondary Quality", "Hot Dipped
                         Galvanized Steel Coils Prime"). F45A on a real LC typically bundles
                         the commodity together with quantity, unit price, and delivery
                         terms all in one block (e.g. "5000 MT COLD ROLLED COILS SECONDARY
                         QUALITY AT USD 593.00 PER M/TON CFR KARACHI") — EXCLUDE all of
                         that: no quantity/weight, no price/rate, no currency, no Incoterm
                         or port ("CFR", "FOB", "CIF KARACHI", etc.), no packing/remarks.
                         Those belong in quantity_mt / unit_price_usd / delivery_terms below
                         — do not duplicate them here even though they share the same F45A text.
quantity_mt          -> total quantity in M/TONS from F45A — the NUMBER ONLY
unit_price_usd        -> the numeric rate per M/TON from F45A — the NUMBER ONLY, e.g. 593.00
                         (do not include "USD" or "PER M/TON")
hs_code              -> HS code if present in the goods / documents
contract_reference   -> any proforma / sales-contract number referenced (e.g. JLXL260327)
delivery_terms       -> Incoterm / basis (e.g. CFR KARACHI)
presentation_period_days -> F48 Period for Presentation in Days (number)
confirmation         -> F49 Confirmation Instructions (e.g. WITHOUT)
charges              -> F71D Charges (short summary)
documents_required   -> F46A documents required (concise summary / comma-separated)
insurance_company    -> name of the insurance company covering the goods, if stated anywhere
                        (often in F46A documents, F47A additional conditions, or a clause like
                        "INSURANCE COVERED BY APPLICANT THROUGH M/S ADAMJEE INSURANCE"). Return the
                        company NAME ONLY (e.g. "Adamjee", "EFU", "Jubilee", "TPL"); null if the LC
                        does not name an insurer (e.g. insurance simply "covered by applicant").

Return ONLY a valid JSON object with these exact keys (null where not found):
{
  "document_type": null, "is_lc": false,
  "lc_number": null, "issue_date": null, "form": null, "applicable_rules": null,
  "expiry_date": null, "expiry_place": null, "applicant_name": null, "beneficiary_name": null,
  "currency": null, "amount": null, "available_with": null, "drafts_at": null,
  "drawee_bank": null, "issuing_bank": null, "advising_bank": null,
  "partial_shipments": null, "transhipment": null,
  "port_of_loading": null, "port_of_discharge": null, "latest_shipment_date": null,
  "goods_description": null, "quantity_mt": null, "unit_price_usd": null, "hs_code": null,
  "contract_reference": null, "delivery_terms": null, "presentation_period_days": null,
  "confirmation": null, "charges": null, "documents_required": null, "insurance_company": null
}

Rules:
1. All dates -> YYYY-MM-DD (SWIFT dates like 260403 = 2026-04-03; "2026 Jun 13" = 2026-06-13).
2. Amounts / quantities / prices -> numbers only, no commas or symbols.
3. lc_number: copy exactly as printed (keep slashes).
4. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_lc(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, LC_PROMPT, api_key, doc_type="lc", max_pages=MAX_LC_PAGES)
    logger.info(f"LC extraction complete — lc_number={data.get('lc_number')}, "
                f"is_lc={data.get('is_lc')}, amount={data.get('amount')}")
    return data
