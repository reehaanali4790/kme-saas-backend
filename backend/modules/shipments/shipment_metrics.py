"""
Shipment quantity / weight / package resolution for reporting.

Priority (never let DPL overwrite final document data):
  1. Commercial Invoice (CI)
  2. Packing List (PL)
  3. Bill of Lading (BL)
  4. DPL — shipment-level totals when a DPL file is on record
  5. Manual shipment / LC fields

DPL is stored as a shipment_documents row (doc_kind=DPL) plus reconciled totals on the
shipment row (total_net_weight_mt, total_gross_weight_mt, total_coils).

Ported from rehan/newchanges (backend/services/shipment_metrics.py).
"""

from __future__ import annotations

from typing import Optional, Tuple

from models.database_models import Shipment, LCMaster


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _primary(docs):
    if not docs:
        return None
    return max(docs, key=lambda d: (
        getattr(d, "invoice_id", None) or getattr(d, "packing_id", None)
        or getattr(d, "bl_id", None) or getattr(d, "gd_id", None) or 0
    ))


def has_dpl(shipment: Shipment) -> bool:
    for d in getattr(shipment, "extra_documents", None) or []:
        if (d.doc_kind or "").upper() == "DPL" and d.document_path:
            return True
    return False


def _lc_qty(lc: Optional[LCMaster]) -> Optional[float]:
    if not lc or not lc.products:
        return None
    vals = [float(p.quantity) for p in lc.products if p.quantity is not None]
    return sum(vals) if vals else None


def _lc_coils(lc: Optional[LCMaster]) -> Optional[int]:
    if not lc or not lc.products:
        return None
    vals = [int(p.pkgs_coils) for p in lc.products if p.pkgs_coils is not None]
    return sum(vals) if vals else None


def resolve_net_weight_mt(shipment: Shipment) -> Tuple[Optional[float], Optional[str]]:
    """Net weight in MT — CI → PL → BL gross → DPL → shipment → LC."""
    inv = _primary(shipment.commercial_invoices)
    if inv and inv.total_net_weight_mt is not None:
        return _f(inv.total_net_weight_mt), "CI"

    pkg = _primary(shipment.packing_lists)
    if pkg and pkg.total_net_weight_mt is not None:
        return _f(pkg.total_net_weight_mt), "PL"

    bl = _primary(shipment.bill_of_ladings)
    if bl and bl.gross_weight_mt is not None:
        return _f(bl.gross_weight_mt), "BL"

    if has_dpl(shipment):
        if shipment.total_net_weight_mt is not None:
            return _f(shipment.total_net_weight_mt), "DPL"
        if shipment.total_gross_weight_mt is not None:
            return _f(shipment.total_gross_weight_mt), "DPL"

    if shipment.total_net_weight_mt is not None:
        return _f(shipment.total_net_weight_mt), "SHP"
    if shipment.delivered_quantity_mt is not None:
        return _f(shipment.delivered_quantity_mt), "SHP"

    lc_qty = _lc_qty(shipment.lc)
    if lc_qty is not None:
        return lc_qty, "LC"
    return None, None


def resolve_gross_weight_mt(shipment: Shipment) -> Tuple[Optional[float], Optional[str]]:
    """Gross weight in MT — CI → PL → BL → DPL → shipment → LC net as last resort."""
    inv = _primary(shipment.commercial_invoices)
    if inv and inv.total_gross_weight_mt is not None:
        return _f(inv.total_gross_weight_mt), "CI"

    pkg = _primary(shipment.packing_lists)
    if pkg and pkg.total_gross_weight_mt is not None:
        return _f(pkg.total_gross_weight_mt), "PL"

    bl = _primary(shipment.bill_of_ladings)
    if bl and bl.gross_weight_mt is not None:
        return _f(bl.gross_weight_mt), "BL"

    if has_dpl(shipment):
        if shipment.total_gross_weight_mt is not None:
            return _f(shipment.total_gross_weight_mt), "DPL"
        if shipment.total_net_weight_mt is not None:
            return _f(shipment.total_net_weight_mt), "DPL"

    if shipment.total_gross_weight_mt is not None:
        return _f(shipment.total_gross_weight_mt), "SHP"
    if shipment.total_net_weight_mt is not None:
        return _f(shipment.total_net_weight_mt), "SHP"

    net, src = resolve_net_weight_mt(shipment)
    if net is not None and src == "LC":
        return net, "LC"
    return None, None


def resolve_coils(shipment: Shipment) -> Tuple[Optional[int], Optional[str]]:
    """Package/coil count — PL → CI → BL → DPL → shipment → LC."""
    pkg = _primary(shipment.packing_lists)
    if pkg and pkg.total_coils is not None:
        return int(pkg.total_coils), "PL"

    inv = _primary(shipment.commercial_invoices)
    if inv and inv.total_coils is not None:
        return int(inv.total_coils), "CI"

    bl = _primary(shipment.bill_of_ladings)
    if bl and bl.package_count is not None:
        return int(bl.package_count), "BL"

    gd = _primary(shipment.goods_declarations)
    if gd and gd.package_count is not None:
        return int(gd.package_count), "GD"

    if has_dpl(shipment) and shipment.total_coils is not None:
        return int(shipment.total_coils), "DPL"

    if shipment.total_coils is not None:
        return int(shipment.total_coils), "SHP"

    lc_coils = _lc_coils(shipment.lc)
    if lc_coils is not None:
        return lc_coils, "LC"
    return None, None


def resolve_amount(shipment: Shipment) -> Tuple[Optional[float], str, Optional[str]]:
    """Invoice amount + currency — CI only, else None with LC currency."""
    inv = _primary(shipment.commercial_invoices)
    lc = shipment.lc
    cur = ((inv.currency if inv and inv.currency else None)
           or (lc.currency if lc else None) or "USD")
    if inv and inv.total_amount_usd is not None:
        return _f(inv.total_amount_usd), cur, "CI"
    return None, cur, None


def resolve_unit_rate(shipment: Shipment,
                      amount: Optional[float] = None,
                      qty: Optional[float] = None) -> Tuple[Optional[float], Optional[str]]:
    """USD per MT — CI unit price, else amount/qty, else LC unit price."""
    inv = _primary(shipment.commercial_invoices)
    if inv and inv.unit_price_usd is not None:
        return _f(inv.unit_price_usd), "CI"

    if amount is not None and qty:
        return round(amount / qty, 2), "DERIVED"

    lc = shipment.lc
    if lc and lc.products:
        rate = next((_f(p.lc_unit_price) for p in lc.products if p.lc_unit_price is not None), None)
        if rate is not None:
            return rate, "LC"
    return None, None


def resolve_container_numbers(shipment: Shipment) -> Tuple[Optional[str], Optional[str]]:
    """Container numbers priority resolution: BL -> DPL -> Manual shipment data -> Dash."""
    bl = _primary(shipment.bill_of_ladings)
    if bl and bl.shipping_marks and ("cntr" in bl.shipping_marks.lower() or "container" in bl.shipping_marks.lower() or "cnu" in bl.shipping_marks.lower()):
        return str(bl.shipping_marks).strip(), "BL"
    if bl and getattr(bl, "raw_extracted_data", None) and isinstance(bl.raw_extracted_data, dict):
        cn = bl.raw_extracted_data.get("container_numbers") or bl.raw_extracted_data.get("container_no")
        if cn:
            return str(cn).strip(), "BL"

    if has_dpl(shipment):
        for d in getattr(shipment, "extra_documents", None) or []:
            if (d.doc_kind or "").upper() == "DPL":
                raw = getattr(d, "raw_extracted_data", None)
                if isinstance(raw, dict):
                    cn = raw.get("container_numbers") or raw.get("container_no") or raw.get("containers")
                    if cn:
                        return str(cn).strip(), "DPL"
                if d.doc_name and ("cntr" in d.doc_name.lower() or "container" in d.doc_name.lower()):
                    return str(d.doc_name).strip(), "DPL"

    if getattr(shipment, "remarks", None) and ("cntr" in shipment.remarks.lower() or "container" in shipment.remarks.lower()):
        return str(shipment.remarks).strip(), "SHP"

    return None, None


def resolve_item_description(shipment: Shipment) -> Tuple[Optional[str], Optional[str]]:
    """Item/goods description — BL -> PL -> CI -> LC product (no DPL text field exists)."""
    bl = _primary(shipment.bill_of_ladings)
    if bl and bl.goods_description:
        return bl.goods_description.strip(), "BL"

    pkg = _primary(shipment.packing_lists)
    if pkg and pkg.goods_description:
        return pkg.goods_description.strip(), "PL"

    inv = _primary(shipment.commercial_invoices)
    if inv and inv.goods_description:
        return inv.goods_description.strip(), "CI"

    lc = shipment.lc
    if lc and lc.products:
        for p in lc.products:
            name = p.item_code or p.product_code or p.product_name
            if name:
                return name, "LC"
    return None, None


def report_weight_coils(shipment: Shipment) -> dict:
    """Weight + coil figures for the Shipment-Wise report DPL column."""
    gross, w_src = resolve_gross_weight_mt(shipment)
    coils, c_src = resolve_coils(shipment)
    containers, cntr_src = resolve_container_numbers(shipment)
    return {
        "dpl_weight_mt": gross,
        "dpl_coils": coils,
        "container_numbers": containers,
        "container_source": cntr_src,
        "weight_source": w_src,
        "coils_source": c_src,
        "using_dpl_fallback": w_src == "DPL" or c_src == "DPL" or cntr_src == "DPL",
    }
