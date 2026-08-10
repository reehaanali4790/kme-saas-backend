"""
Packing List extractor using Claude Vision.
Extracts header fields + the line-item table (size breakdown, no prices).
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

PACKING_PROMPT = """You are a trade-document parser. Extract the PACKING LIST data for an
international steel shipment.

IMPORTANT — this file may be a COMBINED document containing several documents
(Commercial Invoice + Packing List + Bill of Lading), often on separate pages.
FIRST locate the page whose heading reads "PACKING LIST" (or "PACKING SLIP"/"P/L").
Extract the line-item table from THAT page only. Do NOT read the invoice table or the
bill-of-lading / container rider table — those are different documents.

Find each field by its label text; DO NOT assume a fixed position. Packing lists come in
many layouts and the columns vary from document to document — extract whatever is present
and use null for anything genuinely absent. Never invent a column that is not there.

HEADER FIELDS:

packing_number       -> "Packing List No", "P/L No", or the "Invoice No" it references
packing_date         -> "Date", "Dated", "Packing Date"
invoice_number       -> the commercial invoice number it references ("Invoice No.")
goods_description    -> description of goods (e.g. "PRIME HOT DIPPED GALVANIZED STEEL COILS")
hs_code              -> "H.S Code", "HS Code No" (e.g. 7210.4990). If the packing page has
                        no HS code, take it from the invoice/BL page of the same document.
                        If several HS codes are listed, join them with a comma.
total_net_weight_mt  -> TOTAL net weight in MT (totals row)
total_gross_weight_mt-> TOTAL gross weight in MT (totals row)
total_coils          -> TOTAL number of coils/packages (totals row)

LINE ITEMS (one object per DATA row of the packing table, NOT the totals row).
Column layouts differ between documents. Common variants:
  - "SIZE" / "DIA" as a single value: "8.0 MM", "5.5MM", "6.50"  (steel wire rod / bar)
  - "SIZE" as thickness x width: "1.5 x 1220", "1.0 X 1220"      (coils/sheets)
  - an optional "GRADE" column: e.g. "SWRH82B-B"  (present on some docs, absent on others)
  - quantity may share a column with net weight ("QTY(MT)/NET WEIGHT")

For each line item extract:
  item_number        -> row number (1, 2, 3...). If the table has no number column, count rows 1..N.
  grade              -> the steel grade text if a GRADE column exists, else null.
  size               -> the size cell EXACTLY as written ("8.0 MM", "1.5 x 1220", "6.50"), else null.
  size_thickness_mm  -> ONLY when size is "thickness x width": "1.5 x 1220" -> 1.5. Else null.
  size_width_mm      -> ONLY when size is "thickness x width": "1.5 x 1220" -> 1220. Else null.
  quantity_mt        -> quantity in MT (null if the doc has no separate quantity column)
  net_weight_mt      -> net weight in MT
  gross_weight_mt    -> gross weight in MT
  number_of_coils    -> coils in that row

Return ONLY a valid JSON object (use null for any field not found):
{
  "packing_number": null, "packing_date": null, "invoice_number": null,
  "goods_description": null, "hs_code": null,
  "total_net_weight_mt": null, "total_gross_weight_mt": null, "total_coils": null,
  "line_items": [
    {"item_number": 1, "grade": null, "size": null,
     "size_thickness_mm": null, "size_width_mm": null, "quantity_mt": null,
     "net_weight_mt": null, "gross_weight_mt": null, "number_of_coils": null}
  ]
}

Non-negotiable rules:
1. All dates -> YYYY-MM-DD. "30-Jan-26"->2026-01-30, "15/01/2026"->2026-01-15.
2. All weights/quantities -> decimal numbers ONLY, no units. If in KGS divide by 1000.
3. Counts (coils, total_coils) and item_number -> integers only.
4. "grade" and "size" are TEXT — keep them verbatim, do not convert to numbers.
5. Only fill size_thickness_mm / size_width_mm when the size is a "thickness x width" pair.
   For a single-value size (e.g. "8.0 MM" diameter) leave thickness/width null but still set "size".
6. line_items: one object PER DATA ROW. Exclude the TOTAL row (its figures go in total_* fields).
7. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_packing(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, PACKING_PROMPT, api_key, doc_type="packing")
    n = len(data.get("line_items") or [])
    logger.info(f"Packing extraction complete — packing_number={data.get('packing_number')}, line_items={n}")
    return data
