"""Frontend route builders for workflow redirects."""
from __future__ import annotations

from typing import Optional


def contract_upload_href() -> str:
    return "/contract-upload"


def contracts_list_href() -> str:
    return "/contracts"


def create_lc_href(contract_id: Optional[int] = None) -> str:
    if contract_id:
        return f"/create-lc?contract_id={contract_id}"
    return "/create-lc"


def lc_detail_href(lc_id: int) -> str:
    return f"/lc-detail?id={lc_id}"


def lc_table_href() -> str:
    return "/lc-table"


def shipments_href() -> str:
    return "/shipments"


def shipment_workflow_href(shipment_id: int) -> str:
    return f"/shipment?id={shipment_id}&tab=workflow"


def shipment_customs_href(shipment_id: int) -> str:
    return f"/shipment?id={shipment_id}&tab=customs"


def doc_upload_href(
    shipment_id: int,
    lc_id: int,
    doc_type: str,
    **extra: str,
) -> str:
    params = f"shipment_id={shipment_id}&lc_id={lc_id}&type={doc_type}"
    for k, v in extra.items():
        if v is not None:
            params += f"&{k}={v}"
    return f"/shipment-doc-upload?{params}"


def redirect_for_required_step(
    required_step: str,
    *,
    shipment_id: Optional[int] = None,
    lc_id: Optional[int] = None,
    contract_id: Optional[int] = None,
    doc_type: Optional[str] = None,
) -> tuple[str, str]:
    """Return (redirect_href, redirect_label) for a blocked required_step."""
    if required_step == "contract":
        return contract_upload_href(), "Upload contract first"
    if required_step == "contract_pick":
        return contracts_list_href(), "Pick a contract first"
    if required_step == "lc":
        if contract_id:
            return create_lc_href(contract_id), "Open LC for this contract"
        return contracts_list_href(), "Open an LC first"
    if required_step == "lc_exists" and lc_id:
        return lc_detail_href(lc_id), "View existing LC"
    if required_step == "shipment" and lc_id:
        return lc_detail_href(lc_id), "Create shipment under LC"
    if required_step == "docs_core" and shipment_id and lc_id:
        dtype = doc_type or "bl"
        labels = {"bl": "Upload BL", "invoice": "Upload invoice", "packing": "Upload packing list"}
        return doc_upload_href(shipment_id, lc_id, dtype), labels.get(dtype, "Upload core document")
    if required_step == "docs_validated" and shipment_id:
        return shipment_workflow_href(shipment_id), "Resolve validation on Workflow"
    if required_step == "gd_started" and shipment_id and lc_id:
        return doc_upload_href(shipment_id, lc_id, "gdview"), "Upload GD View"
    if required_step == "gd_hc" and shipment_id and lc_id:
        return doc_upload_href(shipment_id, lc_id, "itemdetails"), "Upload item details"
    if required_step == "gd_ib" and shipment_id and lc_id:
        return doc_upload_href(shipment_id, lc_id, "intobondgd"), "Upload Into-Bond GD"
    if required_step == "gd_advance" and shipment_id:
        return shipment_customs_href(shipment_id), "Advance GD one stage at a time"
    if shipment_id:
        return shipment_workflow_href(shipment_id), "Continue workflow"
    if contract_id:
        return create_lc_href(contract_id), "Continue with LC"
    return contracts_list_href(), "Go to contracts"
