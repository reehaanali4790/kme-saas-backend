"""
Cross-document validation engine for a shipment.
Compares BL vs Invoice vs Packing vs LC and records PASS/FAIL/WARNING checks.
Also runs the LC-level short-shipment variance check.

This is the enterprise core: banks reject LC presentations on document mismatches —
this surfaces them before submission.
"""

import logging
import re
from decimal import Decimal

from sqlalchemy.orm import Session

from infrastructure.normalization.smart_match import names_equivalent

from models.database_models import (
    Shipment, BillOfLading, CommercialInvoice, PackingList,
    LCMaster, LCProduct, DocumentValidation, FinancialInstrument,
    Contract,
)
from modules.shipments.services import recompute_shipment_status
from modules.shipments.shipment_metrics import resolve_container_numbers
from modules.shipments.docs_reception import docs_reception_summary
from modules.workflow.import_paths import is_lc_backed, normalize_import_mode


def _norm_hs(code):
    """Normalise an HS code to digits only for comparison (7210.4990 -> 72104990)."""
    if not code:
        return None
    digits = re.sub(r"\D", "", str(code))
    return digits or None

logger = logging.getLogger("uvicorn")

WEIGHT_TOLERANCE_MT = Decimal("0.5")   # acceptable +/- between documents
AMOUNT_TOLERANCE_USD = Decimal("1")
_ISO_CONTAINER = re.compile(r"\b[A-Z]{4}\d{7}\b", re.I)


def _f(v):
    return Decimal(str(v)) if v is not None else None


def _primary(items):
    """Pick the primary document: verified/saved before unsaved PENDING_REVIEW
    placeholders, then most recently created. Mirrors shipment_endpoints._ordered."""
    if not items:
        return None
    return sorted(
        items,
        key=lambda d: (
            1 if getattr(d, "status", None) == "PENDING_REVIEW" else 0,
            -(d.created_at.timestamp() if getattr(d, "created_at", None) else 0),
        ),
    )[0]


def _fmt(v):
    return None if v is None else (f"{float(v):.3f}" if isinstance(v, (Decimal, float)) else str(v))


class _Check:
    def __init__(self, name, ctype):
        self.name, self.ctype = name, ctype
        self.bl = self.inv = self.pkg = self.lc = None
        self.status = "SKIPPED"
        self.message = ""


def _weights_match(values):
    """True if all non-None values are within tolerance of each other."""
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None  # not enough to compare
    return (max(present) - min(present)) <= WEIGHT_TOLERANCE_MT


def _container_count(shipment, bl) -> int | None:
    text, _src = resolve_container_numbers(shipment)
    blob = " ".join(x for x in (text, getattr(bl, "shipping_marks", None) if bl else None) if x)
    found = _ISO_CONTAINER.findall(blob or "")
    if found:
        return len({c.upper() for c in found})
    if bl and bl.package_type and "CONT" in str(bl.package_type).upper() and bl.package_count:
        return int(bl.package_count)
    return None


def _group_status(pairs, kind="generic"):
    """pairs: list of (label, value). Returns (comparable: bool, matched: bool|None)."""
    present = [(lab, val) for lab, val in pairs if val]
    if len(present) < 2:
        return False, None
    base = present[0][1]
    matched = all(_lenient_match(base, val, kind=kind) for _, val in present[1:])
    return True, bool(matched)


def _lenient_match(a, b, kind: str = "generic"):
    """Lenient reference match — delegates to smart fuzzy matcher.
    Returns None when either side is empty."""
    return names_equivalent(a, b, kind=kind)  # type: ignore[arg-type]


def _num_lead(v):
    """Pull the leading number out of a value like '593.00 PER M/TON' or 97,252."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[-+]?\d[\d,]*\.?\d*", str(v))
    return float(m.group().replace(",", "")) if m else None


def cross_check_lc_vs_contract(extracted: dict, contract_id: int, db: Session) -> list[str]:
    """Pre-save cross-check of a freshly extracted LC (SWIFT 700) against its contract.
    Returns human-readable warnings (empty = all clear). Advisory only — warn, allow
    override; lenient matching. Surfaces the classic contract-vs-LC price gap."""
    c = db.query(Contract).filter(Contract.contract_id == contract_id).first()
    if not c:
        return []
    e = extracted or {}
    w: list[str] = []

    ref = e.get("contract_reference")
    if ref and c.contract_number and _lenient_match(ref, c.contract_number, "reference") is False:
        w.append(f"LC's referenced contract '{ref}' does not match this contract '{c.contract_number}'.")
    if e.get("beneficiary_name") and c.supplier_name and _lenient_match(e["beneficiary_name"], c.supplier_name, "company") is False:
        w.append(f"LC beneficiary '{e['beneficiary_name']}' differs from contract supplier '{c.supplier_name}'.")
    if e.get("applicant_name") and c.buyer_name and _lenient_match(e["applicant_name"], c.buyer_name, "company") is False:
        w.append(f"LC applicant '{e['applicant_name']}' differs from contract importer '{c.buyer_name}'.")
    if e.get("currency") and c.currency and _lenient_match(e["currency"], c.currency, "generic") is False:
        w.append(f"LC currency '{e['currency']}' differs from contract currency '{c.currency}'.")

    lc_qty = _num_lead(e.get("quantity_mt"))
    c_qty = sum(float(it.weight_mt or 0) for it in c.items) or None
    if lc_qty and c_qty and abs(lc_qty - c_qty) > 0.5:
        w.append(f"LC quantity {lc_qty} MT differs from contract total {c_qty} MT.")

    lc_price = _num_lead(e.get("unit_price_usd"))
    c_price = _num_lead(c.items[0].lc_price) if c.items else None
    if lc_price and c_price and abs(lc_price - c_price) > 0.01:
        w.append(f"LC unit price {lc_price} differs from contract price {c_price} per MT.")

    if e.get("port_of_loading") and c.port_of_loading and _lenient_match(e["port_of_loading"], c.port_of_loading, "location") is False:
        w.append(f"LC port of loading '{e['port_of_loading']}' differs from contract '{c.port_of_loading}'.")

    return w


def cross_check_gd_extracted(extracted: dict, shipment_id: int, db: Session) -> list[str]:
    """Pre-save cross-check of a freshly extracted GD against the shipment's other documents.
    Returns human-readable warning strings (empty = all clear). Advisory only — the caller
    surfaces these as a 'save anyway?' alert; nothing is blocked. Lenient matching."""
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        return []

    warnings: list[str] = []
    lc = db.query(LCMaster).filter(LCMaster.lc_id == s.lc_id).first() if s.lc_id else None
    bl = _primary(s.bill_of_ladings)
    fi = _primary(s.financial_instruments)

    # GD's LC / financial-instrument number vs the shipment's LC and FI
    gd_lc = (extracted or {}).get("financial_instrument_no")
    if gd_lc:
        refs = [("LC", lc.lc_number if lc else None),
                ("Financial Instrument", fi.fi_number if fi else None)]
        refs = [(name, val) for name, val in refs if val]
        if refs and not any(_lenient_match(gd_lc, val) for _, val in refs):
            ref_txt = ", ".join(f"{name} '{val}'" for name, val in refs)
            warnings.append(f"GD's LC / financial-instrument number '{gd_lc}' does not match "
                            f"the shipment's {ref_txt}.")

    # GD's B/L number vs the shipment's Bill of Lading
    gd_bl = (extracted or {}).get("bl_number")
    if gd_bl and bl and bl.bl_number and _lenient_match(gd_bl, bl.bl_number) is False:
        warnings.append(f"GD's B/L number '{gd_bl}' does not match the shipment's "
                        f"Bill of Lading '{bl.bl_number}'.")

    return warnings


def validate_shipment(shipment_id: int, db: Session) -> dict:
    """Run all checks for a shipment, persist results, update shipment validation_status."""
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        raise ValueError("Shipment not found")

    bl = _primary(s.bill_of_ladings)
    inv = _primary(s.commercial_invoices)
    pkg = _primary(s.packing_lists)
    gd = _primary(s.goods_declarations)
    fi = _primary(s.financial_instruments)
    lc = db.query(LCMaster).filter(LCMaster.lc_id == s.lc_id).first() if s.lc_id else None
    lc_backed = is_lc_backed(s.import_mode)
    contract = db.query(Contract).filter(Contract.contract_id == s.contract_id).first() if s.contract_id else None
    rec = docs_reception_summary(s)

    checks: list[_Check] = []

    # ---- 0. On port without BL (critical) ----
    c = _Check("BL Present When On Port", "DATE")
    if rec.get("on_port") and not bl:
        c.status, c.message = "FAIL", "Vessel is on port but no Bill of Lading is recorded."
    elif rec.get("on_port") and bl:
        c.status, c.message = "PASS", "Bill of Lading recorded while vessel is on port."
    else:
        c.status, c.message = "SKIPPED", "Vessel not on port."
    checks.append(c)

    # ---- 0b. On port without packing (warning) ----
    c = _Check("Packing When On Port", "DATE")
    if rec.get("on_port") and not pkg:
        c.status, c.message = "WARNING", "Vessel on port — packing list not yet received."
    else:
        c.status, c.message = "SKIPPED", "Not applicable."
    checks.append(c)

    # ---- 0c. Non-LC contract vs invoice value ----
    if not lc_backed and contract and inv and inv.total_amount_usd and contract.items:
        c = _Check("Invoice vs Contract Value", "PRICE")
        ctr_amt = sum(
            float(i.lc_amount or i.purchase_amount or 0) for i in contract.items
        )
        inv_amt = float(inv.total_amount_usd)
        if ctr_amt and inv_amt > ctr_amt * 1.5:
            c.status, c.message = "WARNING", (
                f"Invoice total {inv_amt:.2f} USD is much higher than contract total {ctr_amt:.2f} USD."
            )
        else:
            c.status, c.message = "PASS", "Invoice total is within reasonable range of contract value."
        checks.append(c)

    # ---- 1. Total coils match (BL vs Invoice vs Packing) ----
    c = _Check("Total Coils Match", "COUNT")
    c.bl = bl.package_count if bl else None
    c.inv = inv.total_coils if inv else None
    c.pkg = pkg.total_coils if pkg else None
    vals = [v for v in (c.bl, c.inv, c.pkg) if v is not None]
    if len(vals) < 2:
        c.status, c.message = "SKIPPED", "Not enough documents to compare coil counts."
    elif len(set(vals)) == 1:
        c.status, c.message = "PASS", f"All documents agree: {vals[0]} coils."
    else:
        c.status, c.message = "FAIL", f"Coil counts differ — BL:{c.bl} Invoice:{c.inv} Packing:{c.pkg}."
    checks.append(c)

    # ---- 2. Net weight match ----
    c = _Check("Net Weight Match", "WEIGHT")
    c.inv = _f(inv.total_net_weight_mt) if inv else None
    c.pkg = _f(pkg.total_net_weight_mt) if pkg else None
    c.bl = _f(bl.net_weight_mt) if (bl and hasattr(bl, "net_weight_mt")) else None
    m = _weights_match([c.bl, c.inv, c.pkg])
    if m is None:
        c.status, c.message = "SKIPPED", "Not enough documents to compare net weight."
    elif m:
        c.status, c.message = "PASS", "Net weights agree within tolerance."
    else:
        c.status, c.message = "FAIL", f"Net weights differ beyond {WEIGHT_TOLERANCE_MT} MT — " \
                                      f"BL:{_fmt(c.bl)} Invoice:{_fmt(c.inv)} Packing:{_fmt(c.pkg)}."
    checks.append(c)

    # ---- 3. Gross weight match ----
    c = _Check("Gross Weight Match", "WEIGHT")
    c.bl = _f(bl.gross_weight_mt) if bl else None
    c.inv = _f(inv.total_gross_weight_mt) if inv else None
    c.pkg = _f(pkg.total_gross_weight_mt) if pkg else None
    m = _weights_match([c.bl, c.inv, c.pkg])
    if m is None:
        c.status, c.message = "SKIPPED", "Not enough documents to compare gross weight."
    elif m:
        c.status, c.message = "PASS", "Gross weights agree within tolerance."
    else:
        c.status, c.message = "FAIL", f"Gross weights differ beyond {WEIGHT_TOLERANCE_MT} MT — " \
                                      f"BL:{_fmt(c.bl)} Invoice:{_fmt(c.inv)} Packing:{_fmt(c.pkg)}."
    checks.append(c)

    # ---- 4. Documentary credit number vs LC number ----
    c = _Check("LC Reference Match", "DESCRIPTION")
    c.inv = inv.documentary_credit_number if inv else None
    c.lc = lc.lc_number if lc else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — LC reference check not applicable."
    elif not c.inv or not c.lc:
        c.status, c.message = "SKIPPED", "Invoice credit number or LC number missing."
    else:
        a, b = c.inv.strip().upper(), c.lc.strip().upper()
        if a == b or a in b or b in a:
            c.status, c.message = "PASS", "Invoice credit number matches the LC."
        else:
            c.status, c.message = "WARNING", f"Invoice credit number '{c.inv}' differs from LC '{c.lc}'."
    checks.append(c)

    # ---- 5. BL date within LC validity (<= expiry / last ship date) ----
    c = _Check("Ship Date Within LC Validity", "DATE")
    bl_date = bl.bl_date if bl else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — LC validity check not applicable."
    elif bl_date and lc:
        c.bl = bl_date.isoformat()
        limit = lc.last_ship_date or lc.expiry_date
        c.lc = limit.isoformat() if limit else None
        if limit and bl_date > limit:
            c.status, c.message = "FAIL", f"BL date {bl_date} is AFTER LC last-ship/expiry {limit}."
        elif limit:
            c.status, c.message = "PASS", f"BL date {bl_date} is within LC validity ({limit})."
        else:
            c.status, c.message = "SKIPPED", "LC has no last-ship/expiry date."
    else:
        c.status, c.message = "SKIPPED", "BL date or LC missing."
    checks.append(c)

    # ---- 6. LC-level short-shipment variance ----
    c = _Check("Quantity Variance (Short Shipment)", "VARIANCE")
    if s.expected_quantity_mt is not None and s.delivered_quantity_mt is not None:
        exp, deliv = _f(s.expected_quantity_mt), _f(s.delivered_quantity_mt)
        var = deliv - exp
        c.lc = _fmt(exp)
        c.inv = _fmt(deliv)
        if var < -WEIGHT_TOLERANCE_MT:
            c.status, c.message = "WARNING", (f"SHORT SHIPMENT: delivered {_fmt(deliv)} MT vs expected "
                                              f"{_fmt(exp)} MT (short by {_fmt(abs(var))} MT).")
        elif var > WEIGHT_TOLERANCE_MT:
            c.status, c.message = "WARNING", (f"OVER SHIPMENT: delivered {_fmt(deliv)} MT vs expected "
                                              f"{_fmt(exp)} MT (over by {_fmt(var)} MT).")
        else:
            c.status, c.message = "PASS", f"Delivered {_fmt(deliv)} MT matches expected {_fmt(exp)} MT."
    else:
        c.status, c.message = "SKIPPED", "Expected or delivered quantity not set."
    checks.append(c)

    # ---- 7. HS Code match (Invoice vs Packing vs GD vs FI vs LC product) ----
    c = _Check("HS Code Match", "DESCRIPTION")
    lc_hs = lc.products[0].hs_code if (lc and lc.products) else None
    sources = {
        "Invoice": inv.hs_code if inv else None,
        "Packing": pkg.hs_code if pkg else None,
        "GD": gd.hs_code if gd else None,
        "FI": fi.hs_code if fi else None,
        "LC": lc_hs,
    }
    c.inv = inv.hs_code if inv else None
    c.pkg = pkg.hs_code if pkg else None
    c.lc = lc_hs
    present = {k: _norm_hs(v) for k, v in sources.items() if _norm_hs(v)}
    if len(present) < 2:
        c.status, c.message = "SKIPPED", "Not enough documents carry an HS code to compare."
    elif len(set(present.values())) == 1:
        c.status, c.message = "PASS", f"HS code consistent across documents: {sources['Invoice'] or next(iter([v for v in sources.values() if v]))}."
    else:
        detail = ", ".join(f"{k}:{sources[k]}" for k in present)
        c.status, c.message = "FAIL", f"HS codes differ across documents — {detail}. Banks reject on HS mismatch."
    checks.append(c)

    # ---- 8. GD LC reference match (GD vs LC / FI) ----
    c = _Check("GD LC Reference Match", "DESCRIPTION")
    gd_lc = gd.financial_instrument_no if gd else None
    refs = [("LC", lc.lc_number if lc else None),
            ("FI", fi.fi_number if fi else None)]
    refs = [(name, val) for name, val in refs if val]
    c.lc = lc.lc_number if lc else None
    if not gd_lc or not refs:
        c.status, c.message = "SKIPPED", "GD has no LC/FI reference, or no LC/FI on the shipment to compare."
    elif any(_lenient_match(gd_lc, val) for _, val in refs):
        c.status, c.message = "PASS", f"GD LC/FI reference '{gd_lc}' matches the shipment."
    else:
        ref_txt = ", ".join(f"{name}:{val}" for name, val in refs)
        c.status, c.message = "WARNING", f"GD LC/FI reference '{gd_lc}' differs from {ref_txt}."
    checks.append(c)

    # ---- 9. GD BL number match (GD vs BL) ----
    c = _Check("GD BL Number Match", "DESCRIPTION")
    gd_bl = gd.bl_number if gd else None
    bl_num = bl.bl_number if bl else None
    c.bl = bl_num
    if not gd_bl or not bl_num:
        c.status, c.message = "SKIPPED", "GD or BL number missing."
    elif _lenient_match(gd_bl, bl_num):
        c.status, c.message = "PASS", f"GD B/L number '{gd_bl}' matches the Bill of Lading."
    else:
        c.status, c.message = "WARNING", f"GD B/L number '{gd_bl}' differs from BL '{bl_num}'."
    checks.append(c)

    prod = lc.products[0] if (lc and lc.products) else None

    # ---- 10. Vessel name (BL / invoice / shipment / LC) ----
    c = _Check("Vessel Name Match", "DESCRIPTION")
    c.bl = bl.vessel_name if bl else None
    c.inv = inv.vessel_name if inv else None
    c.lc = lc.vessel_name if lc else (s.vessel_name if s else None)
    comparable, matched = _group_status([
        ("BL", c.bl), ("Invoice", c.inv), ("Shipment", s.vessel_name), ("LC", lc.vessel_name if lc else None),
    ], kind="vessel")
    if not comparable:
        c.status, c.message = "SKIPPED", "Not enough vessel names to compare."
    elif matched:
        c.status, c.message = "PASS", "Vessel name is consistent across documents."
    else:
        c.status, c.message = "FAIL", (
            f"Vessel names differ — BL:{_fmt(c.bl)} Invoice:{_fmt(c.inv)} "
            f"Shipment:{_fmt(s.vessel_name)} LC:{_fmt(lc.vessel_name if lc else None)}."
        )
    checks.append(c)

    # ---- 11. Port of discharge ----
    c = _Check("Port of Discharge Match", "DESCRIPTION")
    c.bl = bl.port_of_discharge if bl else None
    c.inv = inv.port_of_discharge if inv else None
    c.lc = lc.arrival_port if lc else (s.port_of_discharge if s else None)
    comparable, matched = _group_status([
        ("BL", c.bl), ("Invoice", c.inv), ("Shipment", s.port_of_discharge),
        ("LC", lc.arrival_port if lc else None),
    ], kind="location")
    if not comparable:
        c.status, c.message = "SKIPPED", "Not enough ports of discharge to compare."
    elif matched:
        c.status, c.message = "PASS", "Port of discharge is consistent across documents."
    else:
        c.status, c.message = "FAIL", (
            f"Ports differ — BL:{_fmt(c.bl)} Invoice:{_fmt(c.inv)} "
            f"Shipment:{_fmt(s.port_of_discharge)} LC:{_fmt(c.lc)}."
        )
    checks.append(c)

    # ---- 12. Consignee vs LC applicant ----
    c = _Check("Consignee vs LC Applicant", "DESCRIPTION")
    c.bl = bl.consignee if bl else None
    c.lc = lc.importer_name if lc else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — consignee/applicant check not applicable."
    elif not c.bl or not c.lc:
        c.status, c.message = "SKIPPED", "BL consignee or LC applicant missing."
    elif _lenient_match(c.bl, c.lc, kind="company"):
        c.status, c.message = "PASS", "BL consignee matches the LC applicant."
    else:
        c.status, c.message = "FAIL", f"BL consignee '{c.bl}' differs from LC applicant '{c.lc}'."
    checks.append(c)

    # ---- 13. Quantity MT vs LC product quantity ----
    c = _Check("Quantity vs LC", "VARIANCE")
    c.inv = _fmt(_f(inv.total_net_weight_mt) if inv else None)
    c.pkg = _fmt(_f(pkg.total_net_weight_mt) if pkg else None)
    c.lc = _fmt(_f(prod.quantity) if prod else None)
    qty_docs = [v for v in (_f(inv.total_net_weight_mt) if inv else None,
                            _f(pkg.total_net_weight_mt) if pkg else None) if v is not None]
    lc_qty = _f(prod.quantity) if prod else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — LC quantity check not applicable."
    elif lc_qty is None or not qty_docs:
        c.status, c.message = "SKIPPED", "LC quantity or document weight missing."
    elif all(abs(q - lc_qty) <= WEIGHT_TOLERANCE_MT for q in qty_docs):
        c.status, c.message = "PASS", f"Document weights match LC quantity {_fmt(lc_qty)} MT."
    else:
        c.status, c.message = "FAIL", (
            f"Quantity MT differs from LC {_fmt(lc_qty)} — Invoice:{c.inv} Packing:{c.pkg}."
        )
    checks.append(c)

    # ---- 14. Invoice value vs LC amount ----
    c = _Check("Invoice Value vs LC Amount", "PRICE")
    inv_amt = _f(inv.total_amount_usd) if inv else None
    lc_amt = _f(prod.lc_amount) if prod else None
    c.inv = _fmt(inv_amt)
    c.lc = _fmt(lc_amt)
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — invoice vs LC amount not applicable."
    elif inv_amt is None or lc_amt is None:
        c.status, c.message = "SKIPPED", "Invoice amount or LC amount missing."
    elif abs(inv_amt - lc_amt) <= AMOUNT_TOLERANCE_USD:
        c.status, c.message = "PASS", "Invoice value matches the LC amount."
    elif inv_amt > lc_amt:
        c.status, c.message = "FAIL", (
            f"Invoice {_fmt(inv_amt)} exceeds LC amount {_fmt(lc_amt)}."
        )
    else:
        c.status, c.message = "WARNING", (
            f"Invoice {_fmt(inv_amt)} is below LC amount {_fmt(lc_amt)}."
        )
    checks.append(c)

    # ---- 15. Container count vs LC ----
    c = _Check("Container Count vs LC", "COUNT")
    found = _container_count(s, bl)
    expected = prod.num_containers if prod else None
    c.bl = str(found) if found is not None else None
    c.lc = str(expected) if expected else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — container count check not applicable."
    elif not expected:
        c.status, c.message = "SKIPPED", "LC has no container count to compare."
    elif found is None:
        c.status, c.message = "SKIPPED", "No container numbers found on the BL/shipment."
    elif found == int(expected):
        c.status, c.message = "PASS", f"Container count {found} matches the LC."
    else:
        c.status, c.message = "FAIL", f"Container count {found} differs from LC {expected}."
    checks.append(c)

    # ---- 16. Country of origin ----
    c = _Check("Country of Origin Match", "DESCRIPTION")
    c.inv = inv.country_of_origin if inv else None
    c.lc = prod.origin if prod else None
    if not lc_backed:
        c.status, c.message = "SKIPPED", "Non-LC import — origin check not applicable."
    elif not c.inv or not c.lc:
        c.status, c.message = "SKIPPED", "Invoice origin or LC origin missing."
    elif _lenient_match(c.inv, c.lc, kind="location"):
        c.status, c.message = "PASS", "Country of origin matches the LC."
    else:
        c.status, c.message = "FAIL", f"Invoice origin '{c.inv}' differs from LC origin '{c.lc}'."
    checks.append(c)

    # ---- 17. Core shipping documents present ----
    c = _Check("Core Documents Present", "DESCRIPTION")
    missing = []
    if not bl:
        missing.append("Bill of Lading")
    if not inv:
        missing.append("Commercial Invoice")
    if not pkg:
        missing.append("Packing List")
    c.bl = "present" if bl else "missing"
    c.inv = "present" if inv else "missing"
    c.pkg = "present" if pkg else "missing"
    if not missing:
        c.status, c.message = "PASS", "BL, invoice, and packing list are on the shipment."
    else:
        c.status, c.message = "WARNING", f"Missing core document(s): {', '.join(missing)}."
    checks.append(c)

    # ---- persist ----
    db.query(DocumentValidation).filter(DocumentValidation.shipment_id == shipment_id).delete()
    for c in checks:
        db.add(DocumentValidation(
            shipment_id=shipment_id, check_name=c.name, check_type=c.ctype,
            bl_value=_fmt(c.bl), invoice_value=_fmt(c.inv),
            packing_value=_fmt(c.pkg), lc_value=_fmt(c.lc),
            status=c.status, message=c.message,
        ))

    # ---- overall validation status ----
    # Validation drives ONLY validation_status (the "All Clear / Discrepant" badge).
    # The shipment's main status is derived separately from milestone dates + documents.
    has_fail = any(c.status == "FAIL" for c in checks)
    has_warn = any(c.status == "WARNING" for c in checks)
    comparable = [c for c in checks if c.status != "SKIPPED"]

    if has_fail or has_warn:
        s.validation_status = "DISCREPANT"
    elif comparable:
        s.validation_status = "ALL_CLEAR"
    else:
        s.validation_status = "PENDING"

    # Re-derive the automatic shipment status (docs may have just changed).
    recompute_shipment_status(s)
    from modules.shipments.docs_reception import recompute_docs_reception_status
    recompute_docs_reception_status(s, db)
    db.commit()

    summary = {"PASS": 0, "FAIL": 0, "WARNING": 0, "SKIPPED": 0}
    for c in checks:
        summary[c.status] += 1
    logger.info(f"Validated shipment {shipment_id}: {summary} -> {s.validation_status}")
    return {
        "shipment_id": shipment_id,
        "validation_status": s.validation_status,
        "summary": summary,
        "checks": [{"check_name": c.name, "check_type": c.ctype, "status": c.status,
                    "message": c.message, "bl_value": _fmt(c.bl), "invoice_value": _fmt(c.inv),
                    "packing_value": _fmt(c.pkg), "lc_value": _fmt(c.lc)} for c in checks],
    }


def _pack_from_rows(s: Shipment, rows) -> dict:
    summary = {"PASS": 0, "FAIL": 0, "WARNING": 0, "SKIPPED": 0}
    checks = []
    for v in rows:
        st = v.status or "SKIPPED"
        if st in summary:
            summary[st] += 1
        checks.append({
            "check_name": v.check_name, "check_type": v.check_type, "status": st,
            "message": v.message,
            "bl_value": v.bl_value, "invoice_value": v.invoice_value,
            "packing_value": v.packing_value, "lc_value": v.lc_value,
        })
    return {
        "shipment_id": s.shipment_id,
        "shipment_ref": s.shipment_ref,
        "lc_id": s.lc_id,
        "validation_status": s.validation_status,
        "summary": summary,
        "checks": checks,
    }


def discrepancy_pack(shipment_id: int, db: Session) -> dict:
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id, Shipment.is_deleted.is_(False)).first()
    if not s:
        raise ValueError("Shipment not found")
    rows = (db.query(DocumentValidation)
              .filter(DocumentValidation.shipment_id == shipment_id)
              .order_by(DocumentValidation.validation_id).all())
    return _pack_from_rows(s, rows)


def discrepancy_pack_for_lc(lc_id: int, db: Session) -> dict:
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise ValueError("LC not found")
    shipments = (db.query(Shipment)
                   .filter(Shipment.lc_id == lc_id, Shipment.is_deleted.is_(False))
                   .order_by(Shipment.created_at.asc()).all())
    packs = []
    fail_total = warn_total = 0
    for s in shipments:
        rows = (db.query(DocumentValidation)
                  .filter(DocumentValidation.shipment_id == s.shipment_id)
                  .order_by(DocumentValidation.validation_id).all())
        pack = _pack_from_rows(s, rows)
        packs.append(pack)
        fail_total += pack["summary"]["FAIL"]
        warn_total += pack["summary"]["WARNING"]
    return {
        "lc_id": lc_id,
        "lc_number": lc.lc_number,
        "fail_count": fail_total,
        "warning_count": warn_total,
        "shipments": packs,
    }
