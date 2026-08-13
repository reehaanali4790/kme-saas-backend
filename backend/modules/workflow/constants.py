"""Workflow action identifiers — no imports from other modules."""

ACTION_UPLOAD_CONTRACT = "upload_contract"
ACTION_UPLOAD_LC = "upload_lc"
ACTION_CREATE_SHIPMENT = "create_shipment"
ACTION_UPLOAD_BL = "upload_bl"
ACTION_UPLOAD_INVOICE = "upload_invoice"
ACTION_UPLOAD_PACKING = "upload_packing"
ACTION_UPLOAD_FI = "upload_fi"
ACTION_UPLOAD_INSURANCE = "upload_insurance"
ACTION_UPLOAD_GD = "upload_gd"
ACTION_UPLOAD_GD_VIEW = "upload_gd_view"
ACTION_UPLOAD_ITEM_DETAILS = "upload_item_details"
ACTION_UPLOAD_FINAL_GD = "upload_final_gd"
ACTION_UPLOAD_INTO_BOND_GD = "upload_into_bond_gd"
ACTION_UPLOAD_EX_BOND_GD = "upload_ex_bond_gd"
ACTION_GD_ADVANCE = "gd_advance"
ACTION_GD_SET_STATUS = "gd_set_status"
ACTION_SET_DELIVERY = "set_delivery_date"

MANAGER_ROLES = frozenset({"ADMIN", "MANAGER"})

# Map doc_type query param → gate action
DOC_TYPE_ACTIONS = {
    "bl": ACTION_UPLOAD_BL,
    "invoice": ACTION_UPLOAD_INVOICE,
    "packing": ACTION_UPLOAD_PACKING,
    "fi": ACTION_UPLOAD_FI,
    "insurance": ACTION_UPLOAD_INSURANCE,
    "gd": ACTION_UPLOAD_GD,
    "gdview": ACTION_UPLOAD_GD_VIEW,
    "itemdetails": ACTION_UPLOAD_ITEM_DETAILS,
    "intobondgd": ACTION_UPLOAD_INTO_BOND_GD,
    "exbondgd": ACTION_UPLOAD_EX_BOND_GD,
}
