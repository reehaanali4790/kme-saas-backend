"""
Marine insurance policy / certificate extractor using Claude Vision.
Targets a marine cargo insurance policy or certificate PDF (insurer's print-out),
which references the related BL, LC and vessel and states the premium.
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

INSURANCE_PROMPT = """You are a document parser. Extract data from this MARINE CARGO
INSURANCE policy / certificate (issued by an insurance company for a sea shipment).
Read ALL pages. Find each field by its label; values may be in tables or free text.

REQUIRED FIELDS:
bl_number           -> Bill of Lading number (labels: "B/L No", "BL No", "Bill of Lading No")
lc_number           -> Letter of Credit / documentary credit number (labels: "L/C No", "LC No", "Credit No")
vessel_name         -> the carrying vessel / steamer name (labels: "Per Vessel", "Vessel", "Conveyance", "Per S.S./M.V.")
certificate_number  -> Insurance Certificate number (labels: "Certificate No", "Cover Note No")
policy_number       -> Insurance Policy number (labels: "Policy No")
net_premium         -> the NET premium amount payable (labels: "Net Premium", "Premium"); number only

OPTIONAL FIELDS (use null if not present):
gross_premium       -> the GROSS premium amount (labels: "Gross Premium", "Total Premium"); number only
insurance_company   -> name of the insurer / underwriter issuing this document
issue_date          -> the policy/certificate issue date
sum_insured         -> the insured amount / sum insured / total insured value; number only
currency            -> currency of the premium / sum insured (e.g. USD, PKR)
voyage_route        -> the voyage / route, e.g. "from <port> to <port>" (labels: "Voyage", "From/To", "Transit")
assured_name        -> the assured / insured party name (labels: "Assured", "Insured", "Name of Assured")

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "bl_number": null, "lc_number": null, "vessel_name": null,
  "certificate_number": null, "policy_number": null, "net_premium": null,
  "gross_premium": null, "insurance_company": null, "issue_date": null, "sum_insured": null,
  "currency": null, "voyage_route": null, "assured_name": null
}

Rules:
1. All dates -> YYYY-MM-DD.
2. Money/amount fields -> number only, no currency word, no commas (e.g. "USD 1,234.50" -> 1234.50).
3. Copy reference numbers (BL/LC/certificate/policy) exactly as printed.
4. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_insurance(file_path: str, api_key: str) -> dict:
    """Extract marine insurance fields from a PDF/image via Claude Vision."""
    data = extract_with_tiers(file_path, INSURANCE_PROMPT, api_key, doc_type="insurance")
    logger.info(f"Insurance extraction complete — cert={data.get('certificate_number')}, "
                f"bl={data.get('bl_number')}, lc={data.get('lc_number')}, "
                f"premium={data.get('net_premium')}")
    return data
