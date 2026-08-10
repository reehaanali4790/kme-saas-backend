"""
SRO quota usage linkage.

Item Details gives us, per GD item: an HS code, an SRO reference and declared/assessed
quantities. An SRO entry (EdbApproval) gives us: a company, a Main SRO number, its Group
(child) SRO numbers, an HS code and an approved quantity.

This module joins the two and works out how much of each quota has been used.

Confidence rules — a low-confidence match is NEVER counted, only reported:
    HIGH   company matches AND the item's SRO ref names the Main or a Group SRO number
    HIGH   company matches AND the HS code matches, and exactly ONE quota fits
    LOW    an SRO/HS reference matched but the company does not, or several quotas fit
           -> reported as "match unclear", left out of the used quantity

Manually entered quota items (SroQuotaItem, added on the GD detail page) are explicit and
always count. When a GD already has a manual item against a quota, auto-matching is skipped
for that GD + quota pair so nothing is counted twice.
"""

import logging
import re
from decimal import Decimal

from sqlalchemy.orm import Session

from models.database_models import (
    EdbApproval, SroGroupNumber, SroQuotaItem, GoodsDeclaration, GDItem,
)
from infrastructure.normalization.normalization_service import company_key

logger = logging.getLogger("uvicorn")


def _f(v):
    return float(v) if v is not None else None


def _d(v):
    return Decimal(str(v)) if v is not None else Decimal("0")


def _digits(s):
    d = re.sub(r"\D", "", str(s or ""))
    return d or None


def _refs_in(text):
    """All number-ish references in an SRO / quota string, digits-only, for comparison.
    '7210.4990 | SRO 565(I)/2006' -> {'72104990', '5651', '2006', ...}"""
    if not text:
        return set()
    return {re.sub(r"\D", "", tok) for tok in re.findall(r"[\d.()/-]{4,}", str(text))
            if re.sub(r"\D", "", tok)}


def item_qty_mt(it: GDItem):
    """Quantity an item consumes, in MT: assessed, else declared, else plain quantity.
    Converts from KG when the unit says so."""
    q = it.assessed_quantity
    if q is None:
        q = it.declared_quantity
    if q is None:
        q = it.quantity
    if q is None:
        return None
    unit = (it.unit or it.unit_type or "").strip().upper()
    val = Decimal(str(q))
    if unit.startswith("KG"):
        val = val / Decimal("1000")
    return val


def _approval_refs(a: EdbApproval, groups):
    """Every reference that identifies this quota: main SRO, group SROs, approval no, HS."""
    refs = set()
    for v in (a.main_sro_no, a.approval_no, a.hs_code):
        if v:
            refs |= {_digits(v)}
    for g in groups:
        for v in (g.group_sro_no, g.hs_code):
            if v:
                refs |= {_digits(v)}
    return {r for r in refs if r}


def _hs_set(a: EdbApproval, groups):
    hs = {_digits(a.hs_code)} if a.hs_code else set()
    hs |= {_digits(g.hs_code) for g in groups if g.hs_code}
    # A group SRO number is itself an HS-style code in this business (e.g. 7210.3010).
    hs |= {_digits(g.group_sro_no) for g in groups if g.group_sro_no}
    if a.main_sro_no:
        hs |= {_digits(a.main_sro_no)}
    return {h for h in hs if h}


def _match_item(it: GDItem, gd: GoodsDeclaration, quotas):
    """Match one GD item to a quota. Returns (approval or None, confidence, reason).
    quotas = [(EdbApproval, [SroGroupNumber], refs:set, hs:set, ckey)]"""
    item_refs = _refs_in(it.sro_no) | _refs_in(it.quota_reference)
    item_hs = _digits(it.hs_code)
    gd_ckey = company_key(gd.importer_name) if gd.importer_name else None

    by_sro, by_hs = [], []
    for a, groups, refs, hs, ckey in quotas:
        sro_hit = bool(item_refs & refs)
        hs_hit = bool(item_hs and item_hs in hs)
        if not (sro_hit or hs_hit):
            continue
        company_hit = bool(ckey and gd_ckey and ckey == gd_ckey)
        (by_sro if sro_hit else by_hs).append((a, company_hit))

    candidates = by_sro or by_hs
    if not candidates:
        return None, None, "No SRO quota matches this item's SRO / HS reference."

    confident = [a for a, company_hit in candidates if company_hit]
    if len(confident) == 1:
        return confident[0], "HIGH", None
    if len(confident) > 1:
        return None, "LOW", (f"Item matches {len(confident)} SRO quotas "
                             f"({', '.join(a.main_sro_no or a.approval_no or '?' for a in confident)}) — "
                             f"cannot tell which one it draws on.")
    # Reference matched but the company on the quota is a different one.
    a = candidates[0][0]
    who = a.company_name or "another company"
    importer = gd.importer_name or "this GD's importer"
    return None, "LOW", (f"Item's SRO/HS reference matches quota "
                         f"'{a.main_sro_no or a.approval_no}' but that quota belongs to "
                         f"{who}, not {importer}.")


def _load_quotas(db: Session):
    approvals = db.query(EdbApproval).all()
    groups_by = {}
    for g in db.query(SroGroupNumber).all():
        groups_by.setdefault(g.approval_id, []).append(g)
    out = []
    for a in approvals:
        groups = groups_by.get(a.approval_id, [])
        out.append((a, groups, _approval_refs(a, groups), _hs_set(a, groups),
                    company_key(a.company_name) if a.company_name else None))
    return out


def compute_usage(db: Session) -> dict:
    """Usage of every SRO quota across all GDs.

    Returns {approval_id: {"used_qty_mt", "lines": [...]}, "_unclear": [...]} where a line is
    one consuming GD item / manual quota item."""
    quotas = _load_quotas(db)
    usage = {a.approval_id: {"used_qty_mt": Decimal("0"), "lines": []} for a, *_ in quotas}
    unclear = []

    # 1. Manual quota items — explicit, always counted.
    manual_pairs = set()
    for qi in db.query(SroQuotaItem).filter(SroQuotaItem.approval_id.isnot(None)).all():
        if qi.approval_id not in usage:
            continue
        q = qi.assessed_qty_mt if qi.assessed_qty_mt is not None else qi.declared_qty_mt
        if q is None:
            continue
        usage[qi.approval_id]["used_qty_mt"] += _d(q)
        gd = qi.gd
        usage[qi.approval_id]["lines"].append({
            "source": "MANUAL", "gd_id": qi.gd_id,
            "gd_number": gd.gd_number if gd else None,
            "company_name": gd.importer_name if gd else None,
            "hs_code": qi.hs_code, "sro_no": None,
            "quantity_mt": _f(_d(q)), "confidence": "HIGH",
        })
        manual_pairs.add((qi.gd_id, qi.approval_id))

    # 2. Auto-match GD items from Item Details.
    items = (db.query(GDItem)
               .join(GoodsDeclaration, GDItem.gd_id == GoodsDeclaration.gd_id)
               .filter((GDItem.sro_no.isnot(None)) | (GDItem.quota_reference.isnot(None))).all())
    for it in items:
        gd = it.gd
        if gd is None:
            continue
        a, conf, reason = _match_item(it, gd, quotas)
        if a is None:
            if conf == "LOW":
                unclear.append({
                    "gd_id": gd.gd_id, "gd_number": gd.gd_number,
                    "company_name": gd.importer_name,
                    "item_number": it.item_number, "hs_code": it.hs_code,
                    "sro_no": it.sro_no, "quota_reference": it.quota_reference,
                    "quantity_mt": _f(item_qty_mt(it)), "reason": reason,
                })
            continue
        if (gd.gd_id, a.approval_id) in manual_pairs:
            continue   # already counted from the manual quota item — don't double count
        qty = item_qty_mt(it)
        if qty is None:
            continue
        usage[a.approval_id]["used_qty_mt"] += qty
        usage[a.approval_id]["lines"].append({
            "source": "ITEM_DETAILS", "gd_id": gd.gd_id, "gd_number": gd.gd_number,
            "company_name": gd.importer_name,
            "item_number": it.item_number, "hs_code": it.hs_code, "sro_no": it.sro_no,
            "quantity_mt": _f(qty), "confidence": "HIGH",
        })

    out = {aid: {"used_qty_mt": _f(v["used_qty_mt"]), "lines": v["lines"]}
           for aid, v in usage.items()}
    out["_unclear"] = unclear
    return out


def gd_sro_usage(gd: GoodsDeclaration, db: Session) -> dict:
    """The SRO / quota picture for ONE GD — what the WeBOC tab shows."""
    quotas = _load_quotas(db)
    rows, warnings = [], []
    for it in sorted(gd.items, key=lambda x: x.item_number or 0):
        if not (it.sro_no or it.quota_reference):
            continue
        a, conf, reason = _match_item(it, gd, quotas)
        qty = item_qty_mt(it)
        rows.append({
            "item_number": it.item_number, "hs_code": it.hs_code,
            "goods_description": it.goods_description,
            "sro_no": it.sro_no, "quota_reference": it.quota_reference,
            "quantity_mt": _f(qty),
            "matched": a is not None,
            "confidence": conf,
            "approval_id": a.approval_id if a else None,
            "main_sro_no": (a.main_sro_no or a.approval_no) if a else None,
            "company_name": a.company_name if a else None,
            "approved_qty_mt": _f(a.approved_qty_mt) if a else None,
            "note": reason,
        })
        if a is None and reason:
            warnings.append(f"Item {it.item_number or ''}: {reason}")
    return {"rows": rows, "warnings": warnings,
            "matched_count": sum(1 for r in rows if r["matched"]),
            "unclear_count": sum(1 for r in rows if not r["matched"])}
