"""Business logic for the unified document / milestone expiries aggregation,
extracted from modules/alerts/expiries_router.py as part of the Phase 4 module rollout.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    LCMaster, Shipment, FinancialInstrument,
    GoodsDeclaration, Contract, EdbApproval, BankLimit,
)
from modules.weboc.helpers.weboc_service import filing_deadline, bond_summary, GD_FILING_DAYS, INTO_BOND_DAYS

# Canonical types exposed to the UI filters
DOC_TYPES = [
    "LC_EXPIRY",
    "LAST_SHIP",
    "FYI",
    "VESSEL_ETA",
    "GD_FILING",
    "INTO_BOND",
    "MATURITY",
    "SRO_QUOTA",
    "CONTRACT",
    "BANK_LIMIT",
]

DOC_LABELS = {
    "LC_EXPIRY": "LC Expiry",
    "LAST_SHIP": "Latest Shipment Date",
    "FYI": "FYI / Financial Instrument",
    "VESSEL_ETA": "Vessel ETA",
    "GD_FILING": "GD Filing Deadline",
    "INTO_BOND": "Into-Bond Deadline",
    "MATURITY": "DA Maturity",
    "SRO_QUOTA": "SRO / EDB Quota",
    "CONTRACT": "Contract Validity",
    "BANK_LIMIT": "Bank Limit Validity",
}


def _tone(days: Optional[int]) -> str:
    if days is None:
        return "unknown"
    if days <= 0:
        return "expired"
    if days <= 7:
        return "critical"
    if days <= 30:
        return "upcoming"
    return "ok"


def _item(
    *,
    doc_type: str,
    expiry_date: date,
    title: str,
    reference: Optional[str] = None,
    lc_id: Optional[int] = None,
    lc_number: Optional[str] = None,
    shipment_id: Optional[int] = None,
    shipment_ref: Optional[str] = None,
    vessel_name: Optional[str] = None,
    entity_id: Optional[int] = None,
    href: Optional[str] = None,
    note: Optional[str] = None,
    today: Optional[date] = None,
) -> dict:
    today = today or date.today()
    days = (expiry_date - today).days
    return {
        "doc_type": doc_type,
        "doc_label": DOC_LABELS.get(doc_type, doc_type),
        "title": title,
        "reference": reference,
        "expiry_date": expiry_date.isoformat(),
        "days_remaining": days,
        "tone": _tone(days),
        "lc_id": lc_id,
        "lc_number": lc_number,
        "shipment_id": shipment_id,
        "shipment_ref": shipment_ref,
        "vessel_name": vessel_name,
        "entity_id": entity_id,
        "href": href,
        "note": note,
    }


def _ship_ref(s: Optional[Shipment]) -> Optional[str]:
    if not s:
        return None
    return s.shipment_ref or f"SH-{s.shipment_id:04d}"


def list_expiries(db: Session, doc_type: Optional[str], tone: Optional[str],
                  search: Optional[str], days_max: Optional[int], include_ok: bool) -> dict:
    """Aggregate expiry / deadline dates across documents and milestones."""
    today = date.today()
    items: list[dict] = []

    type_filter = None
    if doc_type:
        type_filter = {t.strip().upper() for t in doc_type.split(",") if t.strip()}

    tone_filter = None
    if tone:
        tone_filter = {t.strip().lower() for t in tone.split(",") if t.strip()}

    # ---- LCs ----
    if not type_filter or "LC_EXPIRY" in type_filter or "LAST_SHIP" in type_filter:
        lcs = db.query(LCMaster).filter(LCMaster.status.in_(["OPEN", "SHIPPED"])).all()
        for lc in lcs:
            if (not type_filter or "LC_EXPIRY" in type_filter) and lc.expiry_date:
                items.append(_item(
                    doc_type="LC_EXPIRY",
                    expiry_date=lc.expiry_date,
                    title=f"LC {lc.lc_number}",
                    reference=lc.lc_number,
                    lc_id=lc.lc_id,
                    lc_number=lc.lc_number,
                    entity_id=lc.lc_id,
                    href=f"/lc-detail?id={lc.lc_id}",
                    note="Letter of Credit expiry",
                    today=today,
                ))
            if (not type_filter or "LAST_SHIP" in type_filter) and lc.last_ship_date:
                items.append(_item(
                    doc_type="LAST_SHIP",
                    expiry_date=lc.last_ship_date,
                    title=f"LC {lc.lc_number}",
                    reference=lc.lc_number,
                    lc_id=lc.lc_id,
                    lc_number=lc.lc_number,
                    entity_id=lc.lc_id,
                    href=f"/lc-detail?id={lc.lc_id}",
                    note="Latest date of shipment under LC",
                    today=today,
                ))

    # ---- FYI / Financial Instrument ----
    if not type_filter or "FYI" in type_filter:
        fis = (db.query(FinancialInstrument)
               .options(joinedload(FinancialInstrument.shipment).joinedload(Shipment.lc))
               .filter(FinancialInstrument.expiry_date.isnot(None)).all())
        for fi in fis:
            s = fi.shipment
            lc = s.lc if s else None
            items.append(_item(
                doc_type="FYI",
                expiry_date=fi.expiry_date,
                title=fi.fi_number or f"FYI-{fi.fi_id}",
                reference=fi.fi_number,
                lc_id=fi.lc_id or (lc.lc_id if lc else None),
                lc_number=lc.lc_number if lc else None,
                shipment_id=fi.shipment_id,
                shipment_ref=_ship_ref(s),
                vessel_name=s.vessel_name if s else None,
                entity_id=fi.fi_id,
                href=f"/shipment?id={fi.shipment_id}" if fi.shipment_id else None,
                note="Last date to file the Goods Declaration",
                today=today,
            ))

    # ---- Shipments: ETA + Maturity ----
    if not type_filter or "VESSEL_ETA" in type_filter or "MATURITY" in type_filter:
        ships = (db.query(Shipment)
                 .options(joinedload(Shipment.lc))
                 .filter(Shipment.is_deleted.is_(False)).all())
        for s in ships:
            # Skip fully delivered for ETA noise; still show maturity if set
            if (not type_filter or "VESSEL_ETA" in type_filter) and s.eta and s.status != "DELIVERED":
                items.append(_item(
                    doc_type="VESSEL_ETA",
                    expiry_date=s.eta,
                    title=s.vessel_name or _ship_ref(s) or f"Shipment {s.shipment_id}",
                    reference=_ship_ref(s),
                    lc_id=s.lc_id,
                    lc_number=s.lc.lc_number if s.lc else None,
                    shipment_id=s.shipment_id,
                    shipment_ref=_ship_ref(s),
                    vessel_name=s.vessel_name,
                    entity_id=s.shipment_id,
                    href=f"/shipment?id={s.shipment_id}",
                    note="Vessel estimated arrival",
                    today=today,
                ))
            if (not type_filter or "MATURITY" in type_filter) and s.maturity_date:
                items.append(_item(
                    doc_type="MATURITY",
                    expiry_date=s.maturity_date,
                    title=_ship_ref(s) or f"Shipment {s.shipment_id}",
                    reference=_ship_ref(s),
                    lc_id=s.lc_id,
                    lc_number=s.lc.lc_number if s.lc else None,
                    shipment_id=s.shipment_id,
                    shipment_ref=_ship_ref(s),
                    vessel_name=s.vessel_name,
                    entity_id=s.shipment_id,
                    href=f"/shipment?id={s.shipment_id}",
                    note="DA / usance payment maturity",
                    today=today,
                ))

    # ---- GD filing + into-bond ----
    if not type_filter or "GD_FILING" in type_filter or "INTO_BOND" in type_filter:
        gds = (db.query(GoodsDeclaration)
               .options(joinedload(GoodsDeclaration.shipment).joinedload(Shipment.lc))
               .filter(GoodsDeclaration.eta.isnot(None)).all())
        for gd in gds:
            s = gd.shipment
            lc = s.lc if s else None
            if not type_filter or "GD_FILING" in type_filter:
                fd = filing_deadline(gd, today)
                # Only surface open filing clocks (not already filed)
                if fd.get("deadline") and not fd.get("filed"):
                    items.append(_item(
                        doc_type="GD_FILING",
                        expiry_date=date.fromisoformat(str(fd["deadline"])[:10]),
                        title=gd.gd_number or f"GD-{gd.gd_id}",
                        reference=gd.gd_number,
                        lc_id=gd.lc_id or (lc.lc_id if lc else None),
                        lc_number=lc.lc_number if lc else None,
                        shipment_id=gd.shipment_id,
                        shipment_ref=_ship_ref(s),
                        vessel_name=s.vessel_name if s else None,
                        entity_id=gd.gd_id,
                        href=f"/shipment?id={gd.shipment_id}" if gd.shipment_id else f"/gd-detail?id={gd.gd_id}",
                        note=f"Arrival + {fd.get('filing_days') or GD_FILING_DAYS} days to file GD",
                        today=today,
                    ))
            if not type_filter or "INTO_BOND" in type_filter:
                bond = bond_summary(gd, db, today)
                if bond.get("applies") and bond.get("deadline"):
                    # Only surface bonds with a live clock. A bond settled within the
                    # 1–2% tolerance still shows a small remaining quantity, so settle
                    # state — not remaining alone — decides whether the clock is done.
                    if not bond.get("is_weight_settled"):
                        items.append(_item(
                            doc_type="INTO_BOND",
                            expiry_date=date.fromisoformat(str(bond["deadline"])[:10]),
                            title=gd.gd_number or f"GD-{gd.gd_id}",
                            reference=gd.gd_number,
                            lc_id=gd.lc_id or (lc.lc_id if lc else None),
                            lc_number=lc.lc_number if lc else None,
                            shipment_id=gd.shipment_id,
                            shipment_ref=_ship_ref(s),
                            vessel_name=s.vessel_name if s else None,
                            entity_id=gd.gd_id,
                            href=f"/shipment?id={gd.shipment_id}" if gd.shipment_id else f"/gd-detail?id={gd.gd_id}",
                            note=f"First IB GD + {INTO_BOND_DAYS} days to settle bonded cargo via Ex-Bond",
                            today=today,
                        ))

    # ---- SRO / EDB quota ----
    if not type_filter or "SRO_QUOTA" in type_filter:
        approvals = db.query(EdbApproval).filter(EdbApproval.end_date.isnot(None)).all()
        for a in approvals:
            items.append(_item(
                doc_type="SRO_QUOTA",
                expiry_date=a.end_date,
                title=a.approval_no or f"SRO-{a.approval_id}",
                reference=a.approval_no,
                entity_id=a.approval_id,
                href="/sro-quota",
                note=getattr(a, "company_name", None) or "EDB quota validity end",
                today=today,
            ))

    # ---- Contracts ----
    if not type_filter or "CONTRACT" in type_filter:
        contracts = db.query(Contract).filter(Contract.valid_to.isnot(None)).all()
        for c in contracts:
            items.append(_item(
                doc_type="CONTRACT",
                expiry_date=c.valid_to,
                title=c.contract_number or f"Contract-{c.contract_id}",
                reference=c.contract_number,
                entity_id=c.contract_id,
                href="/contracts",
                note="Sales contract validity",
                today=today,
            ))

    # ---- Bank limits ----
    if not type_filter or "BANK_LIMIT" in type_filter:
        limits = db.query(BankLimit).filter(BankLimit.valid_to.isnot(None)).all()
        for lim in limits:
            bank = lim.bank_name or "Bank"
            items.append(_item(
                doc_type="BANK_LIMIT",
                expiry_date=lim.valid_to,
                title=f"{bank} — {lim.group_company or 'Group'}",
                reference=bank,
                entity_id=lim.limit_id,
                href="/bank-limits",
                note="Bank LC limit validity",
                today=today,
            ))

    # ---- Filters ----
    if not include_ok:
        items = [i for i in items if i["tone"] != "ok"]
    if tone_filter:
        items = [i for i in items if i["tone"] in tone_filter]
    if days_max is not None:
        items = [i for i in items if i["days_remaining"] is not None and i["days_remaining"] <= days_max]
    if search:
        q = search.strip().upper()
        items = [
            i for i in items
            if q in (i.get("title") or "").upper()
            or q in (i.get("reference") or "").upper()
            or q in (i.get("lc_number") or "").upper()
            or q in (i.get("shipment_ref") or "").upper()
            or q in (i.get("vessel_name") or "").upper()
            or q in (i.get("doc_label") or "").upper()
        ]

    items.sort(key=lambda i: (i["days_remaining"] if i["days_remaining"] is not None else 99999, i["doc_type"]))

    counts = {"expired": 0, "critical": 0, "upcoming": 0, "ok": 0, "total": len(items)}
    by_type: dict[str, int] = {}
    for i in items:
        counts[i["tone"]] = counts.get(i["tone"], 0) + 1
        by_type[i["doc_type"]] = by_type.get(i["doc_type"], 0) + 1

    return {
        "today": today.isoformat(),
        "counts": counts,
        "by_type": by_type,
        "doc_types": [{"id": k, "label": DOC_LABELS[k]} for k in DOC_TYPES],
        "items": items,
    }
