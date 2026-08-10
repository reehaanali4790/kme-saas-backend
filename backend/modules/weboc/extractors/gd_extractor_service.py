"""
Goods Declaration (GD-I) extractor using Claude Vision.
Record-only: extracts a filed GD's identity, valuation (declared + assessed),
and the duty breakdown (9 levies).
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

GD_PROMPT = """You are a customs-document parser. Extract data from this Pakistan
GOODS DECLARATION (GD-I / Bill of Entry, filed on FBR WEBOC).

Find each field by its label anywhere on the document. Read the WHOLE form including
the duty/levy table and the valuation rows. Note many forms have TWO value columns:
"Declared" (by importer) and "Assessed" (by customs) — capture both.

declaration_type     -> Field 2 "DECLARATION TYPE": exactly "IB" (into-bond),
                        "HC" (home consumption) or "EX" (ex-bond) as printed.

IDENTITY:
gd_number            -> the GD / declaration number — the PRIMARY identifier of this GD.
                        WeBOC GD numbers commonly look like "KEWB-IB-15823-30-04-2026"
                        (Collectorate-Type-Serial-Date, where Type is HC / IB / EX) or
                        "C-AI-MB-000677". Capture this here EVEN IF it looks machine-generated.
machine_number       -> Field 58 "MACHINE NO. & DATE" — the machine / GD identifier line(s)
                        (e.g. "KEWB-IB-234-" or "AJ WB-IE-14358"). Do NOT put the date here.
field58_date         -> Field 58 date ONLY — the DD-MM-YYYY date printed in the lower-left
                        "58. MACHINE NO. & DATE" box (e.g. "03-07-2026" = 3 July 2026).
                        Return exactly as printed (DD-MM-YYYY). Ignore Box 65 C/F/D date,
                        assessment date, B/L date, and the filename.
filing_date          -> SAME as field58_date but output as YYYY-MM-DD. Pakistan uses DD-MM-YYYY:
                        03-07-2026 is 3 July 2026, NOT March 7. Never use US MM-DD order.
assessment_date      -> the customs assessment date
custom_office        -> "Custom Office", "MCC Appraisement ..."
bank_code            -> bank code (e.g. SBL)
girm_egm_number      -> "GIRM No", "EGM No" reference

FINANCIAL INSTRUMENT (the LC):
financial_instrument_no    -> LC / financial instrument number (e.g. SBL-IMP-155518)
financial_instrument_date  -> its date
financial_instrument_value -> its value (USD)

PARTIES:
importer_ntn         -> importer NTN number (e.g. 7229843)
importer_name        -> consignee / importer name
exporter_name        -> exporter / shipper name

TRANSPORT:
vessel_name          -> ocean vessel name
bl_number            -> B/L number
bl_date              -> B/L date
port_of_shipment     -> port of shipment / loading
port_of_discharge    -> port of discharge
payment_terms        -> e.g. "With LC"

CARGO:
hs_code              -> HS code (e.g. 7210.4990)
goods_description    -> description of goods
package_count        -> Field 31 "NUMBER OF PACKAGES" (an integer count, e.g. 122 — NOT the
                        weight in KGS and NOT the per-item quantity).
package_type         -> the exact package type word as printed on the document (e.g. METAL, COILS, BUNDLES, PACKAGES). Never default or substitute "Steel Coils" if the document specifies "Metal" or another type.
gross_weight_mt      -> gross weight in MT (if KGS divide by 1000)
net_weight_mt        -> net weight in MT (if KGS divide by 1000)
country_of_origin    -> origin country

VALUATION (capture BOTH declared and assessed):
declared_unit_value_usd  -> declared unit value (per kg or per MT as printed)
assessed_unit_value_usd  -> assessed unit value
declared_value_usd       -> declared total value USD
assessed_value_usd       -> assessed total value USD
declared_value_pkr       -> declared customs value PKR
assessed_value_pkr       -> assessed customs value PKR
exchange_rate_pkr        -> USD->PKR exchange rate used
freight_value_usd        -> freight
insurance_value_usd      -> insurance
landing_charges_pct      -> landing charges %

DUTY BREAKDOWN (PKR) — the levy table (codes 1-9):
customs_duty_pkr               -> Customs Duty (CD)
sales_tax_pkr                  -> Sales Tax (ST)
income_tax_pkr                 -> Income Tax / Withholding (IT/WHT)
additional_customs_duty_pkr    -> Additional Customs Duty (ACD)
additional_sales_tax_pkr       -> Additional Sales Tax (AST)
regulatory_duty_pkr            -> Regulatory Duty (RD)
igm_deblocking_pkr             -> IGM Deblocking
extra_pkr                      -> Extra
invoice_not_found_penalty_pkr  -> Invoice Not Found Penalty
total_duties_pkr               -> Total of all duties

ITEMS (fields 37-48) — the GD has ONE OR MORE numbered items. Return one object per item
that actually has data (ignore blank item rows). For EACH item capture:
  item_number              -> Field 37 "ITEM NO"
  hs_code                  -> Field 41 "HS Code"
  goods_description        -> Field 42 "ITEM DESCRIPTION OF GOODS" (full text)
  quantity                 -> Field 38 "QUANTITY" (number only)
  unit_type                -> Field 38(a) "Unit Type" (e.g. KG)
  country_of_origin        -> Field 39 "CO CODE"
  sro_no                   -> Field 40 "SRO NO" — include ALL SRO/notification references
                              shown for the item (join multiple lines with " | ")
  unit_value_declared      -> Field 43 "UNIT VALUE" Declared
  unit_value_assessed      -> Field 43 "UNIT VALUE" Assessed
  total_value_declared_usd -> Field 44 "TOTAL VALUE" Declared
  total_value_assessed_usd -> Field 44 "TOTAL VALUE" Assessed
  custom_value_declared_pkr-> Field 45 "CUSTOM VALUE (PKR)" Declared
  custom_value_assessed_pkr-> Field 45 "CUSTOM VALUE (PKR)" Assessed
  levies                   -> Fields 46/47/48 "LEVY / RATE / SUM PAYABLE" for THIS item:
                              a list of {"code": <e.g. CD/ST/RD/ACD/AST/IT>,
                              "rate": <percent number, e.g. 10.0>, "amount_pkr": <number>}

levy_breakdown -> the GD-level levy totals from the footer "Revenue Recovery" table
                  (field 59/60). Include EVERY code shown there, INCLUDING ones not in the
                  per-item table (e.g. WSc). List of {"code", "rate" (null if not shown),
                  "amount_pkr"}.

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "declaration_type": null,
  "gd_number": null, "machine_number": null, "field58_date": null, "filing_date": null, "assessment_date": null,
  "custom_office": null, "bank_code": null, "girm_egm_number": null,
  "financial_instrument_no": null, "financial_instrument_date": null, "financial_instrument_value": null,
  "importer_ntn": null, "importer_name": null, "exporter_name": null,
  "vessel_name": null, "bl_number": null, "bl_date": null,
  "port_of_shipment": null, "port_of_discharge": null, "payment_terms": null,
  "hs_code": null, "goods_description": null, "package_count": null, "package_type": null,
  "gross_weight_mt": null, "net_weight_mt": null, "country_of_origin": null,
  "declared_unit_value_usd": null, "assessed_unit_value_usd": null,
  "declared_value_usd": null, "assessed_value_usd": null,
  "declared_value_pkr": null, "assessed_value_pkr": null, "exchange_rate_pkr": null,
  "freight_value_usd": null, "insurance_value_usd": null, "landing_charges_pct": null,
  "customs_duty_pkr": null, "sales_tax_pkr": null, "income_tax_pkr": null,
  "additional_customs_duty_pkr": null, "additional_sales_tax_pkr": null,
  "regulatory_duty_pkr": null, "igm_deblocking_pkr": null, "extra_pkr": null,
  "invoice_not_found_penalty_pkr": null, "total_duties_pkr": null,
  "items": [],
  "levy_breakdown": []
}

Rules:
1. filing_date and field58_date come ONLY from Field 58 (Machine No. & Date). All dates -> YYYY-MM-DD
   in output EXCEPT field58_date which stays DD-MM-YYYY as printed.
2. Weights -> MT decimals (KGS / 1000).
3. All money/PKR values -> numbers only, no commas or symbols.
4. Unit values: keep as printed (per kg or per MT).
5. Levy rates -> percent numbers only (e.g. "10.00 %" -> 10.0).
6. "items" and "levy_breakdown" are arrays — return [] if none found, never null.
7. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_gd(file_path: str, api_key: str) -> dict:
    # max_tokens raised above the shared default: multi-item GDs with per-item levy
    # arrays produce a much larger JSON than a header-only extraction.
    data = extract_with_tiers(file_path, GD_PROMPT, api_key, doc_type="gd")
    logger.info(f"GD extraction complete — gd_number={data.get('gd_number')}, "
                f"type={data.get('declaration_type')}, items={len(data.get('items') or [])}, "
                f"total_duties_pkr={data.get('total_duties_pkr')}")
    return data
