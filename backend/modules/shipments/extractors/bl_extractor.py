"""
Bill of Lading extractor using Claude Vision API.
Accepts image (JPG/PNG) or PDF files — PDFs are converted page-by-page
to PNG images using PyMuPDF, then all pages are sent to Claude together.

Page rendering, the token budget and JSON parsing all come from services.document_ai, the
shared extraction core, so the BL benefits from the same truncation retry / JSON repair as
every other document type.
"""

import logging

from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

EXTRACTION_PROMPT = """You are a document parser working on a Bill of Lading (B/L) shipping document.

STEP 1 — CLASSIFY THE DOCUMENT FIRST.
A Bill of Lading is a CARRIER-issued ocean/transport document: it has a B/L number, shipper,
consignee, notify party, vessel/voyage, port of loading and discharge. It is NOT any of these:
  - a Goods Declaration (GD) / customs declaration (has GD number, NTN, customs office, duties)
  - an Import General Manifest (IGM) (a customs vessel manifest / index of B/Ls)
  - a Commercial Invoice (has invoice number, unit prices, amounts)
  - a Packing List (has a packing breakdown, no carrier/consignee block)
  - a Financial Instrument / LC
Set these two fields accordingly:
  document_type     → the ACTUAL kind of document, e.g. "Bill of Lading", "Goods Declaration",
                      "Import General Manifest (IGM)", "Commercial Invoice", "Packing List",
                      "Financial Instrument", or "Other".
  is_bill_of_lading → true ONLY if this document is (or clearly contains) a real Bill of Lading.
                      false for a GD, IGM, invoice, packing list, or anything else.
If is_bill_of_lading is false, set all the other fields to null (do not guess B/L values from a
non-B/L document).

STEP 2 — if it IS a Bill of Lading, extract the fields below.

IMPORTANT — this file may be a COMBINED document containing several documents
(Commercial Invoice + Packing List + Bill of Lading), often on separate pages.
You are receiving ONLY the Bill of Lading section (the relevant page(s) have already
been selected). Read ALL provided pages before extracting — a B/L often spans a main
page plus a rider/attachment listing coils or containers.

The document may span multiple pages — read ALL pages before extracting.
Find each field by searching for its label text anywhere in the document.
DO NOT assume any fixed layout or position — labels can appear anywhere.

FIELDS AND THEIR LABEL VARIATIONS (search the entire document for these):

bl_number        → labeled: "B/L No", "B/L No.", "B/L Number", "Bill of Lading No", "Bill of Lading No.", "B/L NO."
bl_date          → labeled: "Dated", "Date of Issue", "Place and date of issue" — extract the date portion only
bl_issue_place   → labeled: "Place of B(s)/L Issue", "Place of Issue", "Place and date of issue" — extract the place name only
original_bl_count → labeled: "Number of original B(s)/L", "No. of Originals", "Number of Original B/L" — find number e.g. THREE(3)=3
shipper_name     → labeled: "Shipper", "Shipper/Exporter"
shipper_address  → the address lines directly under the shipper name
consignee        → labeled: "Consignee", "Consigned to the order of", "Consignee/Endorsement"
notify_party     → labeled: "Notify Address", "Notify Party", "Also Notify" — full name and address block
carrier_name     → labeled: "Carrier", "CARRIER:" — if no explicit label, use the prominent shipping company name on the document
shipping_agent   → labeled: "Signed for the Carrier", "As Agent for the Carrier", "Agent to contact for release of goods", "Agent for release of goods"
vessel_name      → labeled: "Ocean Vessel", "Vessel", "Ocean Vessel/Voy.No." — ship name ONLY, never include voyage number
voyage_number    → from the same vessel field — the voyage code after a space, "/" or " - " separator (e.g. "ERICA V.A5N500"→V.A5N500, "LISBON EXPRESS / 606E"→606E, "MSC LORENA - 1W607R"→1W607R)
pre_carriage_by  → labeled: "Pre-carriage by", "Pre-Carriage by"
place_of_receipt → labeled: "Place of Receipt by Pre-Carrier", "Place of Receipt"
port_of_loading  → labeled: "Port of Loading"
port_of_discharge → labeled: "Port of Discharge"
final_destination → labeled: "Final Destination", "Place of Delivery", "Final destination (if goods to be transhipped at port of discharge)"
freight_payable_at → labeled: "Freight Payable At", "Freight payable at"
shipping_marks   → labeled: "Marks & Nos", "Marks & Numbers", "Container Nos", "Shipping Marks", "Color Mark"
package_count    → from "Number and kind of packages" column, "Total Say:", "Total Packages (in words)", or RIDER page coil/unit count — integer only. Prefer the total coil/unit count over container count when available.
package_type     → the exact package type word as printed on the document — e.g. METAL, COILS, ROLLS, BUNDLES, SHEETS, PACKAGES, PALLETS, LOT. Extract "METAL" when specified; never default or substitute "Steel Coils" if the document specifies "Metal", "Packages", "Bundles", or another package type.
goods_description → the full description of goods from the cargo table or RIDER page
bl_type          → classify the shipment as "CONTAINER" or "COIL" based on the document's actual content:
                     - "CONTAINER" when you see one or more container numbers (ISO 6346 format: 4 letters
                       + 7 digits, e.g. MSCU1234567), a "Container No." field that is filled in, or terms
                       like FCL, LCL, CY/CY, TEU, FEU, 20FT/40FT/20'/40' container sizes, "said to contain
                       N container(s)".
                     - "COIL" when the cargo table/description is coils, reels, steel coils, HR/CR coils,
                       or other bulk/break-bulk cargo with NO container numbers or container-size markings
                       anywhere in the document.
                     - If truly ambiguous or neither signal is present, return null — do not guess.
gross_weight_mt  → labeled: "Gross Weight", "Gross Weight MT", "GR WT(KGS)", "Total Gross Weight" — in MT. If value is in KGS divide by 1000
net_weight_mt    → labeled: "Net Weight", "Total Net Weight", "NET WT(KGS)" — in MT. If value is in KGS divide by 1000
measurement_m3   → labeled: "Measurement", "Measurement m3", "CBM" — decimal or null
applicant_ntn    → labeled: "Applicant's N.T.N. No", "NTN No", "NTN:", "Applicant NTN", "N.T.N." — the number after the label
freight_terms    → search for "FREIGHT PREPAID" or "FREIGHT COLLECT" anywhere. If neither found (e.g. "AS PER AGREEMENT"), return null
shipped_on_board_clause → search for "CLEAN SHIPPED ON BOARD", "SHIPPED ON BOARD", "Clean on Board" text

Return ONLY a valid JSON object with these exact keys (use null for any field not found):
{
  "document_type": null, "is_bill_of_lading": false,
  "bl_number": null, "bl_date": null, "bl_issue_place": null, "original_bl_count": null,
  "shipper_name": null, "shipper_address": null, "consignee": null, "notify_party": null,
  "carrier_name": null, "shipping_agent": null, "vessel_name": null, "voyage_number": null,
  "pre_carriage_by": null, "place_of_receipt": null, "port_of_loading": null,
  "port_of_discharge": null, "final_destination": null, "freight_payable_at": null,
  "shipping_marks": null, "package_count": null, "package_type": null,
  "goods_description": null, "gross_weight_mt": null, "net_weight_mt": null,
  "measurement_m3": null, "applicant_ntn": null, "freight_terms": null,
  "shipped_on_board_clause": null, "bl_type": null
}

Non-negotiable rules:
1. bl_number: Copy EXACTLY character by character — every slash, letter, digit as printed. Never auto-correct.
2. All dates → YYYY-MM-DD. Handle all formats: "15/01/2026"→2026-01-15, "19-Feb-2026"→2026-02-19, "20260508"→2026-05-08.
3. original_bl_count: integer only. "THREE(3)"=3, "TWO(2)"=2.
4. gross_weight_mt / net_weight_mt: decimal number ONLY. If in KGS divide by 1000 (83855 KGS = 83.855 MT).
5. package_count: integer only. "125 COILS"=125, "ONE HUNDRED AND TWENTY-FIVE COILS ONLY"=125.
6. vessel_name: ship name only. voyage_number: voyage code only.
7. bl_type: exactly "CONTAINER", "COIL", or null — no other values.
8. Return ONLY the JSON object — no explanation, no markdown, no code fences."""



def extract_bl_from_image(file_path: str, api_key: str) -> dict:
    """
    Extract BL fields from an image or PDF file via Claude Vision.
    PDFs are converted page-by-page and all pages sent in one request.
    Raises ExtractionError (user-safe message + raw detail for logs) on failure.
    """
    data = extract_with_tiers(file_path, EXTRACTION_PROMPT, api_key, doc_type="bl")
    logger.info(f"BL extraction complete - bl_number={data.get('bl_number')}")
    return data
