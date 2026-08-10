"""
Report Master — column catalog.

Fields are namespaced as ``source:key`` (e.g. ``shipment:eta``, ``gd:bond_state``)
so users can mix columns from Shipment, BL, GD and LC on one master row (joined
via shipment_id / lc_id).

Ported from rehan/newchanges (backend/services/report_field_catalog.py).
"""

from __future__ import annotations

from typing import Any

SOURCE_LABELS = {
    "shipment": "Shipment & Hub",
    "bl": "Bill of Lading",
    "gd": "Goods Declaration",
    "lc": "LC Product Line",
    "insurance": "Marine Insurance",
    "contract": "Purchase Contract",
}

# Legacy single-source grains (still supported for old saved templates)
LEGACY_GRAINS = ("shipment", "bl", "gd", "lc", "insurance", "contract")
GRAINS = ("master",) + LEGACY_GRAINS

MASTER_GRAIN = "master"

# Each field: key, label, group, type (text|date|number|status|bool)
SHIPMENT_FIELDS = [
    {"key": "shipment_id", "label": "Shipment ID", "group": "Reference", "type": "number"},
    {"key": "lc_number", "label": "LC Number", "group": "Reference", "type": "text"},
    {"key": "shipment_ref", "label": "Shipment Ref", "group": "Reference", "type": "text"},
    {"key": "lot_number", "label": "Lot Number", "group": "Reference", "type": "text"},
    {"key": "bl_number", "label": "BL Number (hub)", "group": "Reference", "type": "text"},
    {"key": "status", "label": "Status", "group": "Status", "type": "status"},
    {"key": "status_label", "label": "Status Label", "group": "Status", "type": "text"},
    {"key": "validation_status", "label": "Validation", "group": "Status", "type": "text"},
    {"key": "vessel_name", "label": "Vessel", "group": "Vessel & Port", "type": "text"},
    {"key": "voyage_number", "label": "Voyage", "group": "Vessel & Port", "type": "text"},
    {"key": "vessel_status", "label": "Vessel Status", "group": "Vessel & Port", "type": "text"},
    {"key": "vessel_location", "label": "Vessel Location", "group": "Vessel & Port", "type": "text"},
    {"key": "country_port", "label": "Country / Port", "group": "Vessel & Port", "type": "text"},
    {"key": "port_of_loading", "label": "Port of Loading", "group": "Vessel & Port", "type": "text"},
    {"key": "port_of_discharge", "label": "Port of Discharge", "group": "Vessel & Port", "type": "text"},
    {"key": "on_port_date", "label": "On Port Date", "group": "Dates", "type": "date"},
    {"key": "departure_date", "label": "Departure Date", "group": "Dates", "type": "date"},
    {"key": "kpt_berth", "label": "KPT Berth", "group": "Vessel & Port", "type": "text"},
    {"key": "bl_date", "label": "BL Date (hub)", "group": "Dates", "type": "date"},
    {"key": "etd", "label": "ETD", "group": "Dates", "type": "date"},
    {"key": "eta", "label": "ETA", "group": "Dates", "type": "date"},
    {"key": "delivery_date", "label": "Delivery Date", "group": "Dates", "type": "date"},
    {"key": "payment_date", "label": "Payment Date", "group": "Dates", "type": "date"},
    {"key": "maturity_date", "label": "Maturity Date", "group": "Dates", "type": "date"},
    {"key": "importer_name", "label": "Importer", "group": "Parties", "type": "text"},
    {"key": "company_code", "label": "Company Code", "group": "Parties", "type": "text"},
    {"key": "party_name", "label": "Party / Buyer", "group": "Parties", "type": "text"},
    {"key": "booked_by", "label": "Booked By", "group": "Parties", "type": "text"},
    {"key": "hoa", "label": "Head of Account", "group": "Parties", "type": "text"},
    {"key": "bank_name", "label": "Bank", "group": "Finance", "type": "text"},
    {"key": "payment_terms", "label": "Payment Terms", "group": "Finance", "type": "text"},
    {"key": "lc_payment_type", "label": "LC Tenor", "group": "Finance", "type": "text"},
    {"key": "currency", "label": "Currency", "group": "Finance", "type": "text"},
    {"key": "lc_rate", "label": "LC Rate (hub)", "group": "Finance", "type": "number"},
    {"key": "lc_amount", "label": "LC Amount", "group": "Finance", "type": "number"},
    {"key": "lme_rate", "label": "LME Rate", "group": "Finance", "type": "number"},
    {"key": "exchange_rate", "label": "Exchange Rate", "group": "Finance", "type": "text"},
    {"key": "item_category", "label": "Item Type", "group": "Cargo", "type": "text"},
    {"key": "product_name", "label": "Product", "group": "Cargo", "type": "text"},
    {"key": "quality", "label": "Quality", "group": "Cargo", "type": "text"},
    {"key": "qty_mt", "label": "Qty (MT)", "group": "Cargo", "type": "number"},
    {"key": "qty_kgs", "label": "Qty (KG)", "group": "Cargo", "type": "number"},
    {"key": "containers", "label": "Containers", "group": "Cargo", "type": "number"},
    {"key": "packages", "label": "Packages / Coils", "group": "Cargo", "type": "number"},
    {"key": "total_gross_weight_mt", "label": "BL Gross Weight (MT)", "group": "Cargo", "type": "number"},
    {"key": "hs_code", "label": "HS Code", "group": "Customs", "type": "text"},
    {"key": "kgtl_total_mt", "label": "KGTL Total (MT)", "group": "Customs", "type": "number"},
    {"key": "kgtl_diff_mt", "label": "KGTL Diff (MT)", "group": "Customs", "type": "number"},
    {"key": "kgtl_diff_status", "label": "KGTL Diff Status", "group": "Customs", "type": "text"},
    {"key": "has_bl", "label": "Has BL", "group": "Documents", "type": "bool"},
    {"key": "has_invoice", "label": "Has Invoice", "group": "Documents", "type": "bool"},
    {"key": "has_packing", "label": "Has Packing", "group": "Documents", "type": "bool"},
    {"key": "has_gd", "label": "Has GD", "group": "Documents", "type": "bool"},
    {"key": "has_fi", "label": "Has FI", "group": "Documents", "type": "bool"},
    {"key": "bl_count", "label": "BL Count", "group": "Documents", "type": "number"},
    {"key": "gd_count", "label": "GD Count", "group": "Documents", "type": "number"},
    {"key": "remarks", "label": "Remarks", "group": "Other", "type": "text"},
]

BL_FIELDS = [
    {"key": "bl_number", "label": "BL Number", "group": "Reference", "type": "text"},
    {"key": "port_of_discharge", "label": "Port of Discharge", "group": "Vessel", "type": "text"},
    {"key": "bl_date", "label": "BL Date", "group": "Dates", "type": "date"},
    {"key": "gross_weight_mt", "label": "Gross Weight (MT)", "group": "Cargo", "type": "number"},
    {"key": "package_type", "label": "Package Type", "group": "Cargo", "type": "text"},
    {"key": "is_container", "label": "Container BL", "group": "Cargo", "type": "bool"},
    {"key": "demurrage_state", "label": "Demurrage State", "group": "Demurrage", "type": "status"},
    {"key": "demurrage_start_date", "label": "Demurrage Start", "group": "Demurrage", "type": "date"},
    {"key": "demurrage_last_free_day", "label": "Last Free Day", "group": "Demurrage", "type": "date"},
    {"key": "demurrage_days_remaining", "label": "Days Remaining", "group": "Demurrage", "type": "number"},
    {"key": "demurrage_chargeable_days", "label": "Chargeable Days", "group": "Demurrage", "type": "number"},
    {"key": "demurrage_total_amount", "label": "Demurrage Paid", "group": "Demurrage", "type": "number"},
    {"key": "demurrage_cleared_date", "label": "Cleared Date", "group": "Demurrage", "type": "date"},
    {"key": "detention_state", "label": "Detention State", "group": "Detention", "type": "status"},
    {"key": "detention_start_date", "label": "Detention Start", "group": "Detention", "type": "date"},
    {"key": "detention_last_free_day", "label": "Detention Last Free Day", "group": "Detention", "type": "date"},
    {"key": "detention_chargeable_days", "label": "Detention Chargeable Days", "group": "Detention", "type": "number"},
    {"key": "detention_total_amount", "label": "Detention Paid", "group": "Detention", "type": "number"},
    {"key": "detention_end_date", "label": "Container Returned", "group": "Detention", "type": "date"},
]

GD_FIELDS = [
    {"key": "gd_number", "label": "GD Number", "group": "Reference", "type": "text"},
    {"key": "gd_type", "label": "GD Type", "group": "Customs", "type": "text"},
    {"key": "gd_status", "label": "GD Status", "group": "Customs", "type": "status"},
    {"key": "declaration_type", "label": "Declaration Type", "group": "Customs", "type": "text"},
    {"key": "item", "label": "Item", "group": "Cargo", "type": "text"},
    {"key": "origin", "label": "Origin", "group": "Cargo", "type": "text"},
    {"key": "ib_gd_date", "label": "GD Filing Date", "group": "Dates", "type": "date"},
    {"key": "ib_expiry_date", "label": "Bond Expiry", "group": "Into-Bond", "type": "date"},
    {"key": "remaining_days", "label": "Bond Days Remaining", "group": "Into-Bond", "type": "number"},
    {"key": "balance_ex_bond_kg", "label": "Balance Ex-Bond (KG)", "group": "Into-Bond", "type": "number"},
    {"key": "bond_state", "label": "Bond State", "group": "Into-Bond", "type": "status"},
    {"key": "is_settled", "label": "Weight Settled", "group": "Into-Bond", "type": "bool"},
    {"key": "penalty_pkr", "label": "Penalty (PKR)", "group": "Finance", "type": "number"},
    {"key": "duties_paid_pkr", "label": "Duties Paid (PKR)", "group": "Finance", "type": "number"},
    {"key": "potential_duty_pkr", "label": "Potential Duty (PKR)", "group": "Finance", "type": "number"},
    {"key": "sro_applicable", "label": "SRO Applicable", "group": "Customs", "type": "bool"},
    {"key": "late_filing", "label": "Late Filing", "group": "Customs", "type": "bool"},
    {"key": "examination_report_available", "label": "Examination Report", "group": "Customs", "type": "bool"},
    {"key": "delivery_status", "label": "Delivery Status", "group": "Status", "type": "text"},
]

LC_FIELDS = [
    {"key": "lc_number", "label": "LC Number", "group": "Reference", "type": "text"},
    {"key": "item", "label": "Item", "group": "Cargo", "type": "text"},
    {"key": "size", "label": "Size", "group": "Cargo", "type": "text"},
    {"key": "grade", "label": "Grade", "group": "Cargo", "type": "text"},
    {"key": "mill_name", "label": "Mill Name", "group": "Cargo", "type": "text"},
    {"key": "buyer", "label": "Buyer", "group": "Parties", "type": "text"},
    {"key": "supplier", "label": "Supplier", "group": "Parties", "type": "text"},
    {"key": "origin", "label": "Origin", "group": "Cargo", "type": "text"},
    {"key": "order_qty_mt", "label": "Order Qty (MT)", "group": "Cargo", "type": "number"},
    {"key": "lc_rate", "label": "LC Rate", "group": "Finance", "type": "number"},
    {"key": "lc_line_amount", "label": "LC Line Amount", "group": "Finance", "type": "number"},
    {"key": "currency", "label": "Currency", "group": "Finance", "type": "text"},
    {"key": "lc_date", "label": "LC Date", "group": "Dates", "type": "date"},
    {"key": "last_ship_date", "label": "Last Ship Date", "group": "Dates", "type": "date"},
    {"key": "lc_status", "label": "LC Status", "group": "Status", "type": "status"},
    {"key": "payment_terms", "label": "Payment Terms", "group": "Finance", "type": "text"},
]

INSURANCE_FIELDS = [
    {"key": "certificate_number", "label": "Certificate No", "group": "Reference", "type": "text"},
    {"key": "policy_number", "label": "Policy No", "group": "Reference", "type": "text"},
    {"key": "insurance_company", "label": "Insurance Company", "group": "Parties", "type": "text"},
    {"key": "issue_date", "label": "Insurance Issue Date", "group": "Dates", "type": "date"},
    {"key": "sum_insured", "label": "Sum Insured", "group": "Finance", "type": "number"},
    {"key": "currency", "label": "Currency", "group": "Finance", "type": "text"},
    {"key": "gross_premium", "label": "Gross Premium", "group": "Finance", "type": "number"},
    {"key": "net_premium", "label": "Net Premium", "group": "Finance", "type": "number"},
    {"key": "premium_rate_pct", "label": "Premium Rate %", "group": "Finance", "type": "number"},
    {"key": "insurance_status", "label": "Insurance Status", "group": "Status", "type": "status"},
]

CONTRACT_FIELDS = [
    {"key": "contract_number", "label": "Contract Number", "group": "Reference", "type": "text"},
    {"key": "status", "label": "Status", "group": "Status", "type": "status"},
    {"key": "source", "label": "Source", "group": "Reference", "type": "text"},
    {"key": "contract_date", "label": "Contract Date", "group": "Dates", "type": "date"},
    {"key": "valid_to", "label": "Valid To", "group": "Dates", "type": "date"},
    {"key": "sent_to_bank_at", "label": "Sent to Bank At", "group": "Dates", "type": "date"},
    {"key": "lc_received_at", "label": "LC Received At", "group": "Dates", "type": "date"},
    {"key": "days_to_lc", "label": "Days to LC", "group": "Finance", "type": "number"},
    {"key": "supplier_name", "label": "Supplier", "group": "Parties", "type": "text"},
    {"key": "buyer_name", "label": "Buyer", "group": "Parties", "type": "text"},
    {"key": "indentor_name", "label": "Indentor", "group": "Parties", "type": "text"},
    {"key": "buyer_ntn", "label": "Buyer NTN", "group": "Parties", "type": "text"},
    {"key": "buyer_address", "label": "Buyer Address", "group": "Parties", "type": "text"},
    {"key": "supplier_address", "label": "Supplier Address", "group": "Parties", "type": "text"},
    {"key": "bank_name", "label": "Bank (Sent To)", "group": "Finance", "type": "text"},
    {"key": "lc_id", "label": "LC ID", "group": "Reference", "type": "number"},
    {"key": "lc_number", "label": "LC Number", "group": "Reference", "type": "text"},
    {"key": "lc_status", "label": "LC Status", "group": "Status", "type": "status"},
    {"key": "payment_terms", "label": "Payment Terms", "group": "Finance", "type": "text"},
    {"key": "currency", "label": "Currency", "group": "Finance", "type": "text"},
    {"key": "delivery_terms", "label": "Delivery Terms", "group": "Finance", "type": "text"},
    {"key": "country_of_origin", "label": "Country of Origin", "group": "Cargo", "type": "text"},
    {"key": "quantity_tolerance", "label": "Quantity Tolerance", "group": "Cargo", "type": "text"},
    {"key": "port_of_loading", "label": "Port of Loading", "group": "Vessel & Port", "type": "text"},
    {"key": "shipping_mark", "label": "Shipping Mark", "group": "Vessel & Port", "type": "text"},
    {"key": "hs_code", "label": "HS Code", "group": "Customs", "type": "text"},
    {"key": "beneficiary_bank", "label": "Beneficiary Bank", "group": "Finance", "type": "text"},
    {"key": "beneficiary_swift", "label": "SWIFT", "group": "Finance", "type": "text"},
    {"key": "beneficiary_account", "label": "Account No", "group": "Finance", "type": "text"},
    {"key": "beneficiary_bank_addr", "label": "Beneficiary Bank Address", "group": "Finance", "type": "text"},
    {"key": "documents_required", "label": "Documents Required", "group": "Other", "type": "text"},
    {"key": "notes", "label": "Notes", "group": "Other", "type": "text"},
    {"key": "item_count", "label": "Line Items", "group": "Cargo", "type": "number"},
    {"key": "product_name", "label": "Product (1st Line)", "group": "Cargo", "type": "text"},
    {"key": "size_description", "label": "Size (1st Line)", "group": "Cargo", "type": "text"},
    {"key": "grade_description", "label": "Grade (1st Line)", "group": "Cargo", "type": "text"},
    {"key": "mill_name", "label": "Mill Name (1st Line)", "group": "Cargo", "type": "text"},
    {"key": "total_weight_mt", "label": "Total Weight (MT)", "group": "Cargo", "type": "number"},
    {"key": "total_lc_amount", "label": "Total L/C Amount", "group": "Finance", "type": "number"},
    {"key": "total_purchase_amount", "label": "Total Purchase Amount", "group": "Finance", "type": "number"},
]

SOURCE_FIELD_MAP: dict[str, list[dict[str, Any]]] = {
    "shipment": SHIPMENT_FIELDS,
    "bl": BL_FIELDS,
    "gd": GD_FIELDS,
    "lc": LC_FIELDS,
    "insurance": INSURANCE_FIELDS,
    "contract": CONTRACT_FIELDS,
}

# Legacy per-grain catalog (unchanged keys for old templates)
CATALOG: dict[str, list[dict[str, Any]]] = SOURCE_FIELD_MAP

MASTER_FILTERS = [
    {"key": "search", "label": "Search", "type": "text"},
    {"key": "status", "label": "Shipment Status", "type": "select", "options_key": "statuses"},
    {"key": "vessel", "label": "Vessel", "type": "select", "options_key": "vessels"},
    {"key": "importer", "label": "Company", "type": "select", "options_key": "importers"},
    {"key": "bank", "label": "Bank", "type": "select", "options_key": "banks"},
    {"key": "item_type", "label": "Item Type", "type": "select", "options_key": "item_types"},
    {"key": "party", "label": "Party", "type": "select", "options_key": "parties"},
    {"key": "eta_from", "label": "ETA From", "type": "date"},
    {"key": "eta_to", "label": "ETA To", "type": "date"},
    {"key": "gd_type", "label": "GD Type", "type": "select", "options_key": "gd_types"},
    {"key": "gd_status", "label": "GD Status", "type": "select", "options_key": "gd_statuses"},
    {"key": "demurrage_state", "label": "Demurrage State", "type": "select", "options_key": "demurrage_states"},
    {"key": "bond_state", "label": "Bond State", "type": "select", "options_key": "bond_states"},
    {"key": "has_gd", "label": "Has GD", "type": "bool"},
    {"key": "has_bl", "label": "Has BL", "type": "bool"},
]

FILTER_DEFS: dict[str, list[dict[str, Any]]] = {
    "master": MASTER_FILTERS,
    "shipment": MASTER_FILTERS[:9],
    "bl": [
        {"key": "state", "label": "Clock State", "type": "select", "options_key": "states"},
        {"key": "vessel", "label": "Vessel", "type": "select", "options_key": "vessels"},
        {"key": "port", "label": "Port", "type": "select", "options_key": "ports"},
        {"key": "importer", "label": "Company", "type": "select", "options_key": "importers"},
        {"key": "date_from", "label": "Start Date From", "type": "date"},
        {"key": "date_to", "label": "Start Date To", "type": "date"},
        {"key": "q", "label": "Search", "type": "text"},
        {"key": "open_only", "label": "Open Only", "type": "bool"},
    ],
    "gd": [
        {"key": "gd_type", "label": "GD Type", "type": "select", "options_key": "gd_types"},
        {"key": "company", "label": "Company", "type": "select", "options_key": "companies"},
        {"key": "bank", "label": "Bank", "type": "select", "options_key": "banks"},
        {"key": "vessel", "label": "Vessel", "type": "select", "options_key": "vessels"},
        {"key": "gd_status", "label": "GD Status", "type": "select", "options_key": "gd_statuses"},
        {"key": "date_from", "label": "Date From", "type": "date"},
        {"key": "date_to", "label": "Date To", "type": "date"},
        {"key": "include_settled", "label": "Include Settled", "type": "bool"},
    ],
    "lc": [
        {"key": "buyer", "label": "Buyer", "type": "select", "options_key": "buyers"},
        {"key": "item", "label": "Item", "type": "select", "options_key": "items"},
        {"key": "origin", "label": "Origin", "type": "select", "options_key": "origins"},
        {"key": "importer", "label": "Company", "type": "select", "options_key": "importers"},
        {"key": "bank", "label": "Bank", "type": "select", "options_key": "banks"},
        {"key": "search", "label": "Search", "type": "text"},
        {"key": "eta_from", "label": "ETA From", "type": "date"},
        {"key": "eta_to", "label": "ETA To", "type": "date"},
    ],
    "contract": [
        {"key": "status", "label": "Contract Status", "type": "select", "options_key": "statuses"},
        {"key": "bank", "label": "Bank", "type": "select", "options_key": "banks"},
        {"key": "supplier", "label": "Supplier", "type": "select", "options_key": "suppliers"},
        {"key": "search", "label": "Search", "type": "text"},
        {"key": "date_from", "label": "Contract Date From", "type": "date"},
        {"key": "date_to", "label": "Contract Date To", "type": "date"},
        {"key": "has_lc", "label": "LC Opened", "type": "bool"},
    ],
}


def qualify_key(source: str, field_key: str) -> str:
    return f"{source}:{field_key}"


def parse_qualified_key(key: str) -> tuple[str | None, str]:
    if ":" in key:
        src, fld = key.split(":", 1)
        return src.lower(), fld
    return None, key


def _qualified_fields() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source, fields in SOURCE_FIELD_MAP.items():
        src_label = SOURCE_LABELS[source]
        for f in fields:
            out.append({
                "key": qualify_key(source, f["key"]),
                "field": f["key"],
                "source": source,
                "source_label": src_label,
                "label": f"{src_label} · {f['label']}",
                "group": f["group"],
                "type": f["type"],
            })
    return out


def get_master_catalog() -> dict:
    fields = _qualified_fields()
    sources = []
    for source, raw_fields in SOURCE_FIELD_MAP.items():
        sources.append({
            "id": source,
            "label": SOURCE_LABELS[source],
            "fields": [
                {**f, "key": qualify_key(source, f["key"])}
                for f in raw_fields
            ],
        })
    return {
        "grain": MASTER_GRAIN,
        "mode": "master",
        "description": (
            "One row per shipment. Pick any column from Shipment, BL, GD or LC — "
            "they are joined automatically via shipment / LC links."
        ),
        "sources": sources,
        "fields": fields,
        "filters": MASTER_FILTERS,
    }


def get_catalog(grain: str | None = None) -> dict:
    if grain and grain.lower() == MASTER_GRAIN:
        return get_master_catalog()
    if grain:
        g = grain.lower()
        if g not in LEGACY_GRAINS:
            raise ValueError(f"Unknown grain: {grain}")
        fields = CATALOG[g]
        groups = sorted({f["group"] for f in fields})
        return {
            "grain": g,
            "mode": "legacy",
            "fields": fields,
            "groups": groups,
            "filters": FILTER_DEFS.get(g, []),
        }
    return {
        "grain": MASTER_GRAIN,
        "mode": "master",
        **{k: v for k, v in get_master_catalog().items() if k != "grain"},
        "legacy_grains": [
            {"id": "shipment", "label": "Shipment only", "description": "Single-source (legacy)"},
            {"id": "bl", "label": "BL only", "description": "Single-source (legacy)"},
            {"id": "gd", "label": "GD only", "description": "Single-source (legacy)"},
            {"id": "lc", "label": "LC only", "description": "Single-source (legacy)"},
            {"id": "contract", "label": "Contract only", "description": "Single-source (legacy)"},
        ],
    }


def field_label(grain: str, key: str) -> str:
    if grain == MASTER_GRAIN or ":" in key:
        src, fld = parse_qualified_key(key)
        if src and src in SOURCE_FIELD_MAP:
            for f in SOURCE_FIELD_MAP[src]:
                if f["key"] == fld:
                    return f"{SOURCE_LABELS[src]} · {f['label']}"
        return key.replace(":", " · ").replace("_", " ").title()
    for f in CATALOG.get(grain, []):
        if f["key"] == key:
            return f["label"]
    return key.replace("_", " ").title()


def valid_column_keys(grain: str) -> set[str]:
    if grain == MASTER_GRAIN:
        return {f["key"] for f in _qualified_fields()}
    return {f["key"] for f in CATALOG.get(grain, [])}


def normalize_columns(grain: str, columns: list[str]) -> tuple[str, list[str]]:
    """Upgrade legacy templates and accept mixed qualified keys."""
    if grain == MASTER_GRAIN:
        valid = valid_column_keys(MASTER_GRAIN)
        cols = [c for c in columns if c in valid]
        return MASTER_GRAIN, cols
    if any(":" in c for c in columns):
        valid = valid_column_keys(MASTER_GRAIN)
        cols = [c for c in columns if c in valid]
        return MASTER_GRAIN, cols
    if grain in LEGACY_GRAINS:
        valid = valid_column_keys(grain)
        cols = [c for c in columns if c in valid]
        return grain, cols
    # legacy shipment columns without prefix → shipment:
    valid = valid_column_keys(MASTER_GRAIN)
    upgraded = []
    for c in columns:
        if c in valid:
            upgraded.append(c)
        elif f"shipment:{c}" in valid:
            upgraded.append(f"shipment:{c}")
    if upgraded:
        return MASTER_GRAIN, upgraded
    return grain, columns
