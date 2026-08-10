"""
WeBOC "Item Details" extractor using Claude Vision.

The Item Details document lists the GD's items with their HS code, quantities
(declared vs assessed) and — the reason we read it — the SRO / EDB / quota references
each item claims. That feeds SRO quota usage tracking.
"""

import logging
from infrastructure.document_ai.document_ai import extract_with_tiers

logger = logging.getLogger("uvicorn")

ITEM_DETAILS_PROMPT = """You are a customs-document parser. Extract data from this Pakistan
WeBOC "ITEM DETAILS" screen / printout (the item-level detail of a Goods Declaration).

Read EVERY item row. Also read the header if it shows the GD number / importer.

HEADER (if shown):
gd_number       -> the GD / declaration number.
importer_name   -> importer / consignee / company name.
importer_ntn    -> importer NTN.
hs_code         -> the main HS code if a single one is shown in the header.

ITEMS -> a list, ONE OBJECT PER ITEM that actually has data (ignore blank rows). For each:
  item_number         -> the item / serial number.
  hs_code             -> the item's HS code (e.g. 7210.4990).
  goods_description   -> the item description of goods (full text).
  quantity            -> the item quantity (number only).
  unit                -> the unit of that quantity (e.g. KG, MT, PCS).
  declared_quantity   -> the DECLARED quantity, when the document shows declared and assessed
                         quantities separately. If only one quantity is shown, put it here.
  assessed_quantity   -> the ASSESSED quantity (as assessed by customs), when shown separately.
  sro_no              -> the SRO number(s) / notification reference(s) claimed by this item.
                         Look for "SRO", "SRO No", "Notification", "Exemption SRO".
                         Include ALL references shown for the item, joined with " | ".
  quota_reference     -> any EDB / quota / approval / concession reference shown for this item
                         (e.g. an EDB approval number, quota certificate no, "V-319").
                         Null if none.
  m_size              -> size / thickness / measurement of item if shown (e.g. "2 mm", "Th: 2mm", "M size: 2 mm").
                         Null if not found.
  country_of_origin   -> origin country of the item.
  unit_value_declared -> declared unit value, if shown.
  unit_value_assessed -> assessed unit value, if shown.
  total_value_declared_usd -> declared total value (USD), if shown.
  total_value_assessed_usd -> assessed total value (USD), if shown.
  custom_value_declared_pkr-> declared customs value (PKR), if shown.
  custom_value_assessed_pkr-> assessed customs value (PKR), if shown.

TOTALS (if the document shows them):
total_quantity   -> total quantity across items (number only).
total_unit       -> the unit of that total.
gross_weight_mt  -> total gross weight in MT (if shown in KGS, divide by 1000).
net_weight_mt    -> total net weight in MT (if shown in KGS, divide by 1000).

Return ONLY a valid JSON object with these exact keys (use null where not found):
{
  "gd_number": null, "importer_name": null, "importer_ntn": null, "hs_code": null,
  "total_quantity": null, "total_unit": null,
  "gross_weight_mt": null, "net_weight_mt": null,
  "items": []
}

Rules:
1. All money/quantity values -> numbers only, no commas or symbols.
2. Weights -> MT decimals (KGS / 1000).
3. "items" is an array — return [] if no item rows are found, never null.
4. Do NOT invent SRO or quota references. Only report what is printed.
5. Return ONLY the JSON object — no explanation, no markdown, no code fences."""


def extract_item_details(file_path: str, api_key: str) -> dict:
    data = extract_with_tiers(file_path, ITEM_DETAILS_PROMPT, api_key, doc_type="item_details")
    items = data.get("items") or []
    logger.info(f"Item Details extraction complete — gd_number={data.get('gd_number')}, "
                f"items={len(items)}, "
                f"sro_refs={[i.get('sro_no') for i in items if i.get('sro_no')]}")
    return data
