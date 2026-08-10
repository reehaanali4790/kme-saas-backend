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

from models.database_models import (
    Shipment, BillOfLading, CommercialInvoice, PackingList,
    LCMaster, LCProduct, DocumentValidation, FinancialInstrument,
    Contract,
)
from modules.shipments.services import recompute_shipment_status


def _norm_hs(code):
    """Normalise an HS code to digits only for comparison (7210.4990 -> 72104990)."""
    if not code:
        return None
    digits = re.sub(r"\D", "", str(code))
    return digits or None

logger = logging.getLogger("uvicorn")

WEIGHT_TOLERANCE_MT = Decimal("0.5")   # acceptable +/- between documents


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


def _lenient_match(a, b):
    """Lenient reference match — case-insensitive, punctuation-insensitive, passes if
    either contains the other (so 'TIANJIN PORT, CHINA' == 'TIANJIN PORT CHINA').
    Returns None when either side is empty."""
    if not a or not b:
        return None
    def norm(s):
        s = re.sub(r"[^A-Za-z0-9]+", " ", str(s)).strip().upper()
        return re.sub(r"\s+", " ", s)
    a, b = norm(a), norm(b)
    if not a or not b:
        return None
    return a == b or a in b or b in a


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
    if ref and c.contract_number and _lenient_match(ref, c.contract_number) is False:
        w.append(f"LC's referenced contract '{ref}' does not match this contract '{c.contract_number}'.")
    if e.get("beneficiary_name") and c.supplier_name and _lenient_match(e["beneficiary_name"], c.supplier_name) is False:
        w.append(f"LC beneficiary '{e['beneficiary_name']}' differs from contract supplier '{c.supplier_name}'.")
    if e.get("applicant_name") and c.buyer_name and _lenient_match(e["applicant_name"], c.buyer_name) is False:
        w.append(f"LC applicant '{e['applicant_name']}' differs from contract importer '{c.buyer_name}'.")
    if e.get("currency") and c.currency and _lenient_match(e["currency"], c.currency) is False:
        w.append(f"LC currency '{e['currency']}' differs from contract currency '{c.currency}'.")

    lc_qty = _num_lead(e.get("quantity_mt"))
    c_qty = sum(float(it.weight_mt or 0) for it in c.items) or None
    if lc_qty and c_qty and abs(lc_qty - c_qty) > 0.5:
        w.append(f"LC quantity {lc_qty} MT differs from contract total {c_qty} MT.")

    lc_price = _num_lead(e.get("unit_price_usd"))
    c_price = _num_lead(c.items[0].lc_price) if c.items else None
    if lc_price and c_price and abs(lc_price - c_price) > 0.01:
        w.append(f"LC unit price {lc_price} differs from contract price {c_price} per MT.")

    if e.get("port_of_loading") and c.port_of_loading and _lenient_match(e["port_of_loading"], c.port_of_loading) is False:
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

    checks: list[_Check] = []

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
    if not c.inv or not c.lc:
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
    if bl_date and lc:
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
