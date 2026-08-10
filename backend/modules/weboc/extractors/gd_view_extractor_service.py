"""
WeBOC "GD View" extractor using Claude Vision.

The GD View is the FIRST WeBOC document in the GD workflow (uploaded before the Item
Details and the Final GD). It carries the declaration type, the ETA, the consignment
identity and the duties/taxes/charges block — including the Section 82 penalty, whose
presence means the GD was filed late.
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

GD_VIEW_PROMPT = """You are a customs-document parser. Extract data from this Pakistan WeBOC
"GD VIEW" screen / printout (the view of a Goods Declaration as filed on FBR WeBOC).

Read the WHOLE document, including every duty, tax, charge and penalty row.

DECLARATION:
declaration_type   -> the declaration type EXACTLY as printed (e.g. "IB", "HC", "EX",
                      "Into Bond", "Home Consumption", "Ex Bond", "Ex-Bond Ex Warehouse").
gd_number          -> the GD / declaration number if shown (e.g. "KEWB-IB-15823-30-04-2026").
                      Null if the GD has no number yet.
filing_date        -> GD filing / declaration date.
custom_office      -> collectorate / custom office.
eta                -> the ETA (Estimated Time of Arrival) / arrival date of the vessel /
                      consignment as shown on this GD View. Look for "ETA", "Arrival Date",
                      "Date of Arrival", "Vessel Arrival". This is important.

PARTIES:
importer_name      -> importer / consignee / company name.
importer_ntn       -> importer NTN.
exporter_name      -> exporter / supplier name.

TRANSPORT:
vessel_name        -> vessel name.
voyage_number      -> voyage number if shown.
bl_number          -> B/L number (Bill of Lading / transport document number).
bl_date            -> B/L date.
port_of_shipment   -> port of shipment / loading.
port_of_discharge  -> port of discharge.

CARGO:
hs_code            -> HS code (main / first one shown).
goods_description  -> description of goods.
package_count      -> number of packages (an integer count).
package_type       -> the exact package type word as printed on the document (e.g. METAL, COILS, BUNDLES, PACKAGES). Never default or substitute "Steel Coils" if the document specifies "Metal" or another type.
gross_weight_mt    -> gross weight in MT (if the document shows KGS, divide by 1000).
net_weight_mt      -> net weight in MT (if the document shows KGS, divide by 1000).
quantity           -> the declared quantity as printed (number only).
unit               -> the unit that quantity is in (e.g. KG, MT).
country_of_origin  -> origin country.

VALUES:
declared_value_pkr -> declared customs value in PKR.
assessed_value_pkr -> assessed customs value in PKR.
declared_value_usd -> declared value in USD.
assessed_value_usd -> assessed value in USD.
exchange_rate_pkr  -> the USD->PKR rate used.

DUTIES / TAXES / CHARGES (PKR) — read the duty & charges table carefully:
customs_duty_pkr               -> Customs Duty (CD)
sales_tax_pkr                  -> Sales Tax (ST)
income_tax_pkr                 -> Income Tax / Withholding (IT / WHT)
additional_customs_duty_pkr    -> Additional Customs Duty (ACD)
additional_sales_tax_pkr       -> Additional Sales Tax (AST)
regulatory_duty_pkr            -> Regulatory Duty (RD)
igm_deblocking_pkr             -> IGM Deblocking
extra_pkr                      -> Extra
total_duties_pkr               -> the TOTAL of all duties/taxes/charges payable.

SECTION 82 PENALTY — VERY IMPORTANT:
section82_penalty_pkr -> the amount of any SECTION 82 penalty. Look in the duty / charges /
                      penalty area for a row labelled any of: "Section 82", "Sec 82", "S.82",
                      "Penalty-F", "Penalty F", "PENALTY", "Late filing penalty",
                      "Surcharge u/s 82". Return the PKR amount of that row.
                      Return null ONLY if no such penalty row exists on the document.
                      A GD carrying this penalty was filed LATE.

charges_breakdown  -> EVERY duty / tax / charge / penalty row in the charges table, in order.
                      A list of {"code": <short label as printed, e.g. "CD","ST","IT","RD",
                      "Section 82 Penalty">, "description": <full label as printed>,
                      "amount_pkr": <number>}.

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "declaration_type": null, "gd_number": null, "filing_date": null, "custom_office": null,
  "eta": null,
  "importer_name": null, "importer_ntn": null, "exporter_name": null,
  "vessel_name": null, "voyage_number": null, "bl_number": null, "bl_date": null,
  "port_of_shipment": null, "port_of_discharge": null,
  "hs_code": null, "goods_description": null, "package_count": null, "package_type": null,
  "gross_weight_mt": null, "net_weight_mt": null, "quantity": null, "unit": null,
  "country_of_origin": null,
  "declared_value_pkr": null, "assessed_value_pkr": null,
  "declared_value_usd": null, "assessed_value_usd": null, "exchange_rate_pkr": null,
  "customs_duty_pkr": null, "sales_tax_pkr": null, "income_tax_pkr": null,
  "additional_customs_duty_pkr": null, "additional_sales_tax_pkr": null,
  "regulatory_duty_pkr": null, "igm_deblocking_pkr": null, "extra_pkr": null,
  "total_duties_pkr": null,
  "section82_penalty_pkr": null,
  "charges_breakdown": []
}

Rules:
1. All dates -> YYYY-MM-DD.
2. Weights -> MT decimals (KGS / 1000).
3. All money values -> numbers only, no commas or currency symbols.
4. "charges_breakdown" is an array — return [] if no charges table is found, never null.
5. Do NOT invent a Section 82 penalty. Only report it if such a row is actually printed.
6. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_gd_view(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, GD_VIEW_PROMPT, api_key, doc_type="gd_view")
    logger.info(f"GD View extraction complete — gd_number={data.get('gd_number')}, "
                f"type={data.get('declaration_type')}, eta={data.get('eta')}, "
                f"section82={data.get('section82_penalty_pkr')}")
    return data
