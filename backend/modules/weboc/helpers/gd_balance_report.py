"""
GD Balance Detail report.

One row per Into-Bond GD: what went into bond, what has been lifted out via Ex-Bond, what
is still sitting there, when its 180-day window runs out, and the duty position —
what has already been paid, and what is still owed on the balance.

Weights are reported in KG (the report sheet's unit) while the database stores MT.
Conversion happens here, in one place.

The bond figures all come from weboc_service.bond_summary — the same function the alert
engine and the WeBOC tab use. That is deliberate: a report that computed its own deadline
would drift away from the alerts, and the two would quietly disagree about the same GD.

    NOTE ON THE 180 vs 179 DAY WINDOW
    The report sheet this was modelled on computes expiry as "IB GD DATE + 179 DAYS",
    i.e. counting the filing date itself as day 1. This system uses IB date + 180
    (weboc_service.INTO_BOND_DAYS). The two differ by one day. This module does NOT
    redefine it — change INTO_BOND_DAYS if 179 is the correct rule, and the report, the
    timer, the alerts and the penalty will all move together.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional, Union

from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    GoodsDeclaration, LCMaster, LCProduct, Shipment,
)
from infrastructure.normalization.normalization_service import (
    norm_bank, company_resolver, matches_company_code,
)
from modules.weboc.helpers.sro_usage import gd_sro_usage
from modules.weboc.helpers.weboc_service import INTO_BOND_DAYS, bond_summary

KG_PER_MT = Decimal("1000")


def _f(v):
    return float(v) if v is not None else None


def _kg(mt) -> Optional[float]:
    """MT -> KG. The database is MT throughout; this report reads in KG."""
    if mt is None:
        return None
    return float(Decimal(str(mt)) * KG_PER_MT)


def _primary_product(lc: Optional[LCMaster]) -> Optional[LCProduct]:
    if not lc or not lc.products:
        return None
    return lc.products[0]


def _buyers(lc: Optional[LCMaster]) -> Optional[str]:
    """Buyer names for the LC. Several allocations -> comma separated."""
    if not lc or not getattr(lc, "buyer_allocations", None):
        return None
    names = [b.buyer_name for b in lc.buyer_allocations if b.buyer_name]
    return ", ".join(dict.fromkeys(names)) or None


def _edb_quota_kg(gd: GoodsDeclaration, db: Session) -> Optional[float]:
    """Quantity this GD draws against an EDB/SRO quota, in KG. Only matched items count —
    an unmatched item is not known to consume any particular quota."""
    usage = gd_sro_usage(gd, db)
    total = Decimal("0")
    found = False
    for r in usage.get("rows", []):
        if r.get("matched") and r.get("quantity_mt") is not None:
            total += Decimal(str(r["quantity_mt"]))
            found = True
    return _kg(total) if found else None


def _gd_rate_per_mt(gd: GoodsDeclaration) -> Optional[float]:
    """The GD's unit value in USD per MT, so it can sit beside the LC rate.

    Derived from total value / weight rather than read off the GD's own unit-value column:
    WeBOC prints the unit value per KG for metals while the LC rate is per MT, so the two
    columns would differ by 1000x and the comparison the report exists for would be
    nonsense. A value-over-weight division is unit-safe whatever the document printed.
    Falls back to the stored unit value when the totals are missing.
    """
    val = gd.assessed_value_usd or gd.declared_value_usd
    wt = gd.gross_weight_mt or gd.net_weight_mt
    if val is not None and wt:
        return float(Decimal(str(val)) / Decimal(str(wt)))
    return _f(gd.assessed_unit_value_usd or gd.declared_unit_value_usd)


def _potential_duty(gd: GoodsDeclaration, bond: dict) -> Optional[float]:
    """Duty still owed on the quantity in bond.

    Duty is paid at the Ex-Bond stage, so whatever is still bonded carries a future
    liability. The IB GD's assessed duty covers the whole consignment, so the balance's
    share of it is that duty apportioned by weight:

        potential = IB total duties x (balance qty / bonded qty)

    None when the GD has no assessed duty, or nothing is left in bond.
    """
    total_duty = gd.total_duties_pkr
    bonded = bond.get("bonded_qty_mt")
    remaining = bond.get("remaining_qty_mt")
    if total_duty is None or not bonded or remaining is None:
        return None
    if remaining <= 0 or bond.get("is_weight_settled"):
        return 0.0
    share = Decimal(str(remaining)) / Decimal(str(bonded))
    return float(Decimal(str(total_duty)) * share)


def _ci_contains(hay, needle: str) -> bool:
    if not needle:
        return True
    if not hay:
        return False
    return needle.strip().upper() in str(hay).upper()


def _delivery_status(gd: GoodsDeclaration) -> Optional[str]:
    s = gd.shipment
    if not s:
        return None
    if s.delivery_date:
        return "DELIVERED"
    st = (s.status or "").upper()
    if st in ("DELIVERED", "CLOSED"):
        return "DELIVERED"
    return "PENDING"


def _hc_is_settled(gd: GoodsDeclaration, delivery_status: Optional[str]) -> bool:
    """HC GDs are complete when the shipment is delivered or the GD is RELEASED."""
    if delivery_status == "DELIVERED":
        return True
    return (gd.status or "").upper() in ("RELEASED", "CLOSED")


def _hc_weight_kg(gd: GoodsDeclaration) -> Optional[float]:
    """Declared weight on the Home Consumption GD itself — not an Into-Bond balance."""
    return _kg(gd.gross_weight_mt or gd.net_weight_mt)


def _hc_duty_position(gd: GoodsDeclaration, delivery_status: Optional[str]) -> tuple:
    """(duties_paid_pkr, potential_duty_pkr, is_settled, duty_recorded).

    HC duty is assessed on the declaration directly. When the linked shipment is
    Delivered (or the GD is RELEASED), the full assessed duty counts as paid."""
    total = _f(gd.total_duties_pkr)
    settled = _hc_is_settled(gd, delivery_status)
    if total is None:
        return None, None, settled, False
    if settled:
        return total, 0.0, True, True
    return 0.0, total, False, True


def _late_filing(gd: GoodsDeclaration) -> bool:
    return bool(gd.late_filed) or float(gd.section82_penalty_pkr or 0) > 0


def _sro_applicable(gd: GoodsDeclaration, db: Session) -> bool:
    return gd_sro_usage(gd, db).get("matched_count", 0) > 0


def _lc_id_of(gd: GoodsDeclaration) -> Optional[int]:
    """The GD's own lc_id, else the one its shipment belongs to. GoodsDeclaration has no
    `lc` relationship — only the column — so the LC is resolved by id, not attribute."""
    if gd.lc_id:
        return gd.lc_id
    return gd.shipment.lc_id if gd.shipment else None


def _row(gd: GoodsDeclaration, db: Session, today: date, lc_map: dict, resolver) -> dict:
    lc = lc_map.get(_lc_id_of(gd))
    s = gd.shipment
    prod = _primary_product(lc)
    gd_type = (gd.gd_type or "").upper()
    delivery = _delivery_status(gd)

    if gd_type == "HOME_CONSUMPTION":
        weight_kg = _hc_weight_kg(gd)
        paid, potential, settled, duty_recorded = _hc_duty_position(gd, delivery)
        importer_raw = (lc.importer_name if lc and lc.importer_name else None) or gd.importer_name
        imp_res = resolver.resolve(importer_raw)
        return {
            "gd_id": gd.gd_id,
            "shipment_id": gd.shipment_id,
            "lc_id": lc.lc_id if lc else None,
            "lc_reference": lc.lc_number if lc else None,
            "item": (prod.item_code or prod.product_code) if prod else None,
            "origin": (prod.origin if prod else None) or gd.country_of_origin,
            "importer": importer_raw,
            "company_code": imp_res["short_code"],
            "company_matched": imp_res["matched"],
            "hs_code": gd.hs_code or (prod.hs_code if prod else None),
            "lc_rate": _f(prod.lc_unit_price) if prod else None,
            "gd_rate": _gd_rate_per_mt(gd),
            "custom_collectorate": gd.custom_office,
            "ib_gd_number": gd.gd_number,
            "ib_gd_date": gd.filing_date.isoformat() if gd.filing_date else None,
            "eb_entries": [],
            "edb_quota_utilized_kg": _edb_quota_kg(gd, db),
            # HC: no bond — balance = GD weight still pending delivery; 0 once delivered.
            "balance_ex_bond_kg": 0.0 if settled else weight_kg,
            "ib_expiry_date": None,
            "remaining_days": None,
            "containers": (int(prod.num_containers)
                           if prod and prod.num_containers is not None else None),
            "packages": gd.package_count,
            "ib_gd_weight_kg": weight_kg,
            "buyer": _buyers(lc),
            "duties_paid_pkr": paid,
            "duties_paid_missing_count": 0,
            "potential_duty_pkr": potential,
            "ib_total_duties_pkr": _f(gd.total_duties_pkr),
            "vessel_name": (s.vessel_name if s else None) or gd.vessel_name,
            "shipment_ref": (s.shipment_ref or f"SH-{s.shipment_id:04d}") if s else None,
            "lifted_kg": None,
            "gd_type": gd.gd_type,
            "gd_status": gd.status,
            "declaration_type": (gd.declaration_type_raw or "").strip() or None,
            "bank_name": norm_bank(lc.bank_name) if lc and lc.bank_name else None,
            "sro_applicable": _sro_applicable(gd, db),
            "late_filing": _late_filing(gd),
            "section82_penalty_pkr": _f(gd.section82_penalty_pkr),
            "delivery_status": delivery,
            "bond_state": None,
            "is_settled": settled,
            "penalty_pkr": None,
            "penalty_applies": False,
            "duty_recorded": duty_recorded,
            "is_verified": bool(gd.is_verified),
            "report_mode": "HOME_CONSUMPTION",
        }

    bond = bond_summary(gd, db, today)

    entries = bond.get("entries", []) if bond.get("applies") else []
    eb_rows = [{
        "gd_number": e.get("gd_number"),
        "ex_bond_date": e.get("ex_bond_date"),
        "quantity_kg": _kg(e.get("quantity_mt")),
        "quantity_mt": e.get("quantity_mt"),
        "duties_paid_pkr": e.get("duties_paid_pkr"),
    } for e in entries]

    paid = sum((Decimal(str(e["duties_paid_pkr"])) for e in eb_rows
                if e.get("duties_paid_pkr") is not None), Decimal("0"))
    paid_known = any(e.get("duties_paid_pkr") is not None for e in eb_rows)
    # Liftings recorded with no duty against them — the paid total is understated until
    # they are filled in, and the report says so rather than implying the total is final.
    paid_missing = sum(1 for e in eb_rows if e.get("duties_paid_pkr") is None)

    importer_raw = (lc.importer_name if lc and lc.importer_name else None) or gd.importer_name
    imp_res = resolver.resolve(importer_raw)

    return {
        "gd_id": gd.gd_id,
        "shipment_id": gd.shipment_id,
        "lc_id": lc.lc_id if lc else None,
        # --- sheet columns ---
        "lc_reference": lc.lc_number if lc else None,
        "item": (prod.item_code or prod.product_code) if prod else None,
        "origin": (prod.origin if prod else None) or gd.country_of_origin,
        "importer": importer_raw,
        "company_code": imp_res["short_code"],
        "company_matched": imp_res["matched"],
        "hs_code": gd.hs_code or (prod.hs_code if prod else None),
        "lc_rate": _f(prod.lc_unit_price) if prod else None,
        "gd_rate": _gd_rate_per_mt(gd),
        "custom_collectorate": gd.custom_office,
        "ib_gd_number": gd.gd_number,
        # IB anchors on the bond start date; HC/EX have no bond clock, so fall back to the
        # GD's own filing date so the date-range filter still works for those types.
        "ib_gd_date": bond.get("ib_start_date") or (gd.filing_date.isoformat() if gd.filing_date else None),
        "eb_entries": eb_rows,
        "edb_quota_utilized_kg": _edb_quota_kg(gd, db),
        "balance_ex_bond_kg": _kg(bond.get("remaining_qty_mt")),
        "ib_expiry_date": bond.get("deadline"),
        "remaining_days": bond.get("days_remaining"),
        # 0 containers is a real answer (bulk cargo), not missing data — don't let a
        # falsy 0 collapse to blank.
        "containers": (int(prod.num_containers)
                       if prod and prod.num_containers is not None else None),
        "packages": gd.package_count,
        "ib_gd_weight_kg": _kg(bond.get("bonded_qty_mt")),
        "buyer": _buyers(lc),
        # --- duty position ---
        "duties_paid_pkr": float(paid) if paid_known else None,
        "duties_paid_missing_count": paid_missing,
        "potential_duty_pkr": _potential_duty(gd, bond),
        "ib_total_duties_pkr": _f(gd.total_duties_pkr),
        # --- context ---
        "vessel_name": (s.vessel_name if s else None) or gd.vessel_name,
        "shipment_ref": (s.shipment_ref or f"SH-{s.shipment_id:04d}") if s else None,
        "lifted_kg": _kg(bond.get("lifted_qty_mt")),
        "gd_type": gd.gd_type,
        "gd_status": gd.status,
        "declaration_type": (gd.declaration_type_raw or "").strip() or None,
        "bank_name": norm_bank(lc.bank_name) if lc and lc.bank_name else None,
        "sro_applicable": _sro_applicable(gd, db),
        "late_filing": _late_filing(gd),
        "section82_penalty_pkr": _f(gd.section82_penalty_pkr),
        "delivery_status": _delivery_status(gd),
        "bond_state": bond.get("state"),
        "is_settled": bool(bond.get("is_weight_settled")),
        "penalty_pkr": bond.get("penalty_pkr"),
        "penalty_applies": bool(bond.get("penalty_applies")),
        "duty_recorded": gd.total_duties_pkr is not None,
        "is_verified": bool(gd.is_verified),
        "report_mode": "INTO_BOND" if gd_type == "INTO_BOND" else "EX_BOND",
    }


def _to_list(v: Optional[Union[str, int, List[str], List[int]]]) -> Optional[List[str]]:
    """Normalise a filter value that may arrive as a single string (older/internal
    callers) or a list (multi-select query params) into a clean list, or None if
    the filter wasn't provided. Keeps single-value call sites (roi_service.py,
    report_master_service.py, diagnostics) working unchanged."""
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        items = v
    else:
        items = [v]
    vals = []
    for x in items:
        if x not in (None, ""):
            for part in str(x).split(","):
                part_clean = part.strip()
                if part_clean:
                    vals.append(part_clean)
    return vals or None


def gd_balance_report(
    db: Session,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    vessel: Optional[Union[str, List[str]]] = None,
    shipment_id: Optional[Union[str, int, List[str], List[int]]] = None,
    gd_id: Optional[int] = None,
    company: Optional[Union[str, List[str]]] = None,
    bank: Optional[Union[str, List[str]]] = None,
    lc_number: Optional[str] = None,
    gd_status: Optional[Union[str, List[str]]] = None,
    declaration_type: Optional[Union[str, List[str]]] = None,
    sro_applicable: Optional[Union[str, List[str]]] = None,
    late_penalty: Optional[Union[str, List[str]]] = None,
    delivery_status: Optional[Union[str, List[str]]] = None,
    include_settled: bool = True,
    gd_type: str = "INTO_BOND",
    today: Optional[date] = None,
) -> dict:
    """GDs of the given type with their bond balance and duty position.

    gd_type is one of HOME_CONSUMPTION / INTO_BOND / EX_BOND. Bond-balance columns
    (expiry clock, remaining ex-bond qty, potential duty) only apply to INTO_BOND;
    for HC/EX they come back blank since those GDs carry no bond.

    Filters: GD date range, vessel, shipment, or a single GD. No filter = all GDs of type.
    vessel/company/bank/gd_status/declaration_type/sro_applicable/late_penalty/delivery_status
    each accept a single string or a list — multiple values are OR'd together.
    """
    today = today or date.today()
    resolver = company_resolver(db)
    vessel = _to_list(vessel)
    shipment_ids = _to_list(shipment_id)
    company = _to_list(company)
    bank = _to_list(bank)
    gd_status = _to_list(gd_status)
    declaration_type = _to_list(declaration_type)
    sro_applicable = _to_list(sro_applicable)
    late_penalty = _to_list(late_penalty)
    delivery_status = _to_list(delivery_status)

    q = (db.query(GoodsDeclaration)
           .options(
               joinedload(GoodsDeclaration.shipment),
               joinedload(GoodsDeclaration.items),
           ))
    if gd_type != "ALL":
        q = q.filter(GoodsDeclaration.gd_type == gd_type)

    if gd_id:
        q = q.filter(GoodsDeclaration.gd_id == gd_id)
    if shipment_ids:
        ship_id_ints = [int(s) for s in shipment_ids if s.strip().lstrip("-").isdigit()]
        if ship_id_ints:
            q = q.filter(GoodsDeclaration.shipment_id.in_(ship_id_ints))

    gds = q.all()

    if vessel:
        vessel_needles = [v.upper() for v in vessel]
        gds = [g for g in gds
               if any(v in ((g.shipment.vessel_name if g.shipment else None) or
                            g.vessel_name or "").upper() for v in vessel_needles)]

    # LCs (with products + buyers) fetched once for the whole report rather than per row.
    lc_ids = {i for i in (_lc_id_of(g) for g in gds) if i}
    lc_map = {}
    if lc_ids:
        lcs = (db.query(LCMaster)
                 .options(joinedload(LCMaster.products),
                          joinedload(LCMaster.buyer_allocations))
                 .filter(LCMaster.lc_id.in_(lc_ids)).all())
        lc_map = {lc.lc_id: lc for lc in lcs}

    rows = [_row(g, db, today, lc_map, resolver) for g in gds]

    # Date filter runs on the IB GD date, which bond_summary resolves (filing date, else
    # the date in the GD number, else ETA) — so it matches the date shown in the row.
    if date_from or date_to:
        kept = []
        for r in rows:
            d = r["ib_gd_date"]
            if not d:
                continue
            dd = date.fromisoformat(d)
            if date_from and dd < date_from:
                continue
            if date_to and dd > date_to:
                continue
            kept.append(r)
        rows = kept

    if not include_settled:
        rows = [r for r in rows if not r["is_settled"]]

    if company:
        rows = [r for r in rows if any(matches_company_code(resolver, r.get("importer"), c) for c in company)]
    if bank:
        bank_set = {b.strip() for b in bank}
        rows = [r for r in rows if r.get("bank_name") in bank_set]
    if lc_number:
        rows = [r for r in rows if _ci_contains(r.get("lc_reference"), lc_number)]
    if gd_status:
        status_set = {s.strip().upper() for s in gd_status}
        rows = [r for r in rows if (r.get("gd_status") or "").upper() in status_set]
    if declaration_type:
        rows = [r for r in rows if any(_ci_contains(r.get("declaration_type"), d) for d in declaration_type)]
    if sro_applicable:
        wants = {s.strip().lower() == "yes" for s in sro_applicable}
        rows = [r for r in rows if bool(r.get("sro_applicable")) in wants]
    if late_penalty:
        wants = {s.strip().lower() == "yes" for s in late_penalty}
        rows = [r for r in rows if bool(r.get("late_filing")) in wants]
    if delivery_status:
        ds_set = {s.strip().upper() for s in delivery_status}
        rows = [r for r in rows if (r.get("delivery_status") or "").upper() in ds_set]

    # Soonest expiry first — the GDs about to breach are the ones worth looking at.
    rows.sort(key=lambda r: (r["remaining_days"] if r["remaining_days"] is not None else 99999,
                             r["lc_reference"] or ""))

    def _sum(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return float(sum(vals)) if vals else 0.0

    open_rows = [r for r in rows if not r["is_settled"]]
    is_hc = gd_type == "HOME_CONSUMPTION"
    totals = {
        "gd_count": len(rows),
        "open_count": len(open_rows),
        "settled_count": len(rows) - len(open_rows),
        "ib_gd_weight_kg": _sum("ib_gd_weight_kg"),
        "lifted_kg": _sum("lifted_kg") if not is_hc else 0.0,
        "balance_ex_bond_kg": float(sum(r["balance_ex_bond_kg"] for r in open_rows
                                        if r.get("balance_ex_bond_kg") is not None)) if open_rows else 0.0,
        "duties_paid_pkr": _sum("duties_paid_pkr"),
        "potential_duty_pkr": _sum("potential_duty_pkr"),
        "penalty_pkr": _sum("penalty_pkr"),
        "rows_missing_paid_duty": sum(1 for r in rows if r["duties_paid_missing_count"] > 0),
        "rows_missing_potential": (
            sum(1 for r in open_rows if not r.get("duty_recorded"))
            if is_hc else
            sum(1 for r in rows if not r["is_settled"] and r["potential_duty_pkr"] is None)
        ),
        "expiring_soon": 0 if is_hc else sum(1 for r in open_rows
                             if r["remaining_days"] is not None and 0 <= r["remaining_days"] <= 20),
        "expired": 0 if is_hc else sum(1 for r in open_rows
                       if r["remaining_days"] is not None and r["remaining_days"] < 0),
        "report_mode": "HOME_CONSUMPTION" if is_hc else ("INTO_BOND" if gd_type == "INTO_BOND" else ("ALL" if gd_type == "ALL" else "EX_BOND")),
    }

    return {
        "today": today.isoformat(),
        "bond_days": INTO_BOND_DAYS,
        "totals": totals,
        "rows": rows,
    }


def gd_balance_filter_options(db: Session, gd_type: str = "INTO_BOND") -> dict:
    """Dropdown values drawn from GDs of the selected type — only options that can return rows."""
    resolver = company_resolver(db)
    q = (db.query(GoodsDeclaration)
             .options(joinedload(GoodsDeclaration.shipment)))
    if gd_type != "ALL":
        q = q.filter(GoodsDeclaration.gd_type == gd_type)
    gds = q.all()
    lc_ids = {i for i in (_lc_id_of(g) for g in gds) if i}
    lc_map = {}
    if lc_ids:
        lcs = db.query(LCMaster).filter(LCMaster.lc_id.in_(lc_ids)).all()
        lc_map = {lc.lc_id: lc for lc in lcs}

    vessels, ships = set(), {}
    companies = set()
    banks, gd_statuses, declaration_types = set(), set(), set()
    for g in gds:
        s = g.shipment
        name = (s.vessel_name if s else None) or g.vessel_name
        if name:
            from infrastructure.normalization.normalization_service import norm_vessel
            vessels.add(norm_vessel(name) or name.strip())
        if s:
            ships[s.shipment_id] = s.shipment_ref or f"SH-{s.shipment_id:04d}"
        lc = lc_map.get(_lc_id_of(g))
        importer = (lc.importer_name if lc and lc.importer_name else None) or g.importer_name
        if importer:
            code = resolver.resolve(importer).get("short_code")
            if code:
                companies.add(code)
        if lc and lc.bank_name:
            banks.add(norm_bank(lc.bank_name))
        if g.status:
            gd_statuses.add(g.status)
        if g.declaration_type_raw:
            declaration_types.add(g.declaration_type_raw.strip())
    return {
        "companies": sorted(companies),
        "banks": sorted(banks),
        "gd_statuses": sorted(gd_statuses),
        "declaration_types": sorted(declaration_types),
        "vessels": sorted(vessels),
        "shipments": [{"shipment_id": k, "label": v} for k, v in
                      sorted(ships.items(), key=lambda kv: kv[1])],
    }
