"""Business logic for the Vessel-Wise / Bank-Wise / Buyer-Wise / GD Balance reports,
extracted from modules/reports/router.py as part of the Phase 4 module rollout.
"""
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from models.database_models import Shipment, LCMaster, LCProduct, Contract
from modules.reports.schemas import VesselBulkUpdate
from modules.shipments.services import shipment_summary
from modules.shipments.vessel_status_service import (
    apply_port_status,
    resolve_vessel_status,
    STANDARD_STATUSES,
    bulk_set_demurrage_start_for_shipments,
)
from modules.shipments.shipment_metrics import (
    report_weight_coils, resolve_net_weight_mt, resolve_coils,
    resolve_container_numbers, resolve_item_description,
)
from infrastructure.normalization.normalization_service import (
    company_resolver, matches_company_code, enrich_company_fields,
)

logger = logging.getLogger("uvicorn")

# Issuing-bank canonicalisation: map noisy free-text bank names (branches, legal suffixes,
# abbreviations) to one clean group name. Order matters — more specific keys first.
_BANK_RULES = [
    (("MCB ISLAMIC",), "MCB Islamic Bank"),
    (("MCB", "MUSLIM COMMERCIAL"), "MCB Bank"),
    (("HABIB METRO", "HABIBMETRO", "HMBL"), "Habib Metro Bank"),
    (("BANK AL-HABIB", "BANK AL HABIB", "AL-HABIB", "AL HABIB", "BAHL"), "Bank Al Habib"),
    (("HABIB BANK", "HBL"), "HBL"),
    (("SONERI",), "Soneri Bank"),
    (("BANKISLAMI", "BANK ISLAMI", "BIPL"), "BankIslami"),
    (("UBL", "UNITED BANK"), "UBL"),
    (("ALBARAKA", "AL BARAKA"), "Al Baraka Bank"),
    (("ASKARI",), "Askari Bank"),
    (("MEEZAN",), "Meezan Bank"),
    (("ALFALAH", "AL FALAH", "BAFL"), "Bank Alfalah"),
    (("ALLIED", "ABL"), "Allied Bank"),
    (("NATIONAL BANK", "NBP"), "National Bank of Pakistan"),
    (("FAYSAL",), "Faysal Bank"),
    (("STANDARD CHARTERED", "SCB"), "Standard Chartered"),
    (("DUBAI ISLAMIC", "DIB"), "Dubai Islamic Bank"),
    (("JS BANK", "JSBL"), "JS Bank"),
    (("SINDH BANK",), "Sindh Bank"),
    (("BANK OF PUNJAB", "BOP"), "Bank of Punjab"),
    (("BANK OF KHYBER",), "Bank of Khyber"),
    (("SUMMIT",), "Summit Bank"),
    (("SILK",), "Silkbank"),
    (("SAMBA",), "Samba Bank"),
    (("CITI",), "Citibank"),
    (("DEUTSCHE",), "Deutsche Bank"),
    (("ICBC", "INDUSTRIAL AND COMMERCIAL"), "ICBC"),
    (("MOBILINK", "MMBL"), "Mobilink Microfinance Bank"),
]


def norm_bank(name):
    """Group an issuing-bank free-text value to one clean bank name."""
    if not name or not str(name).strip():
        return "(Unknown Bank)"
    u = re.sub(r"\s+", " ", str(name).upper()).strip()
    for keys, canon in _BANK_RULES:
        for k in keys:
            if k in u:
                return canon
    # fallback: drop branch (parens / after comma) + legal suffixes, title-case the core
    s = u.split("(")[0].split(",")[0]
    s = re.sub(r"\b(LIMITED|LTD|PVT|PRIVATE|PLC|PAKISTAN|BRANCH|BR|HEAD OFFICE)\b", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s else "(Unknown Bank)"


def da_sight(payment_terms):
    """Short DA / SIGHT tenor label from the LC payment-terms free text (falls back to
    the raw value trimmed). NOTE: derived from lc_master.payment_terms — no dedicated
    tenor column exists; if a structured tenor field is added later, map it here."""
    if not payment_terms or not str(payment_terms).strip():
        return None
    u = str(payment_terms).upper()
    if "SIGHT" in u:
        return "SIGHT"
    if re.search(r"\bDA\b", u) or "USANCE" in u or "ACCEPTANCE" in u or "DAYS" in u:
        return "DA"
    return str(payment_terms).strip()


def _normalize_list(v) -> list[str]:
    """Split comma-separated values in query list filters and strip whitespace."""
    if not v:
        return []
    if isinstance(v, str):
        v = [v]
    res = []
    for item in v:
        if item:
            for part in str(item).split(","):
                p = part.strip()
                if p:
                    res.append(p)
    return res


# Tracked shipment documents (V1 auto-detect). (key, label, relationship attr, is_critical)
DOC_TYPES = [
    ("bl",        "Bill of Lading",       "bill_of_ladings",        True),
    ("invoice",   "Commercial Invoice",   "commercial_invoices",    True),
    ("packing",   "Packing List",         "packing_lists",          False),
    ("gd",        "GD",                   "goods_declarations",     True),
    ("fi",        "Financial Instrument", "financial_instruments",  False),
    ("insurance", "Insurance",            "insurance_certificates", False),
]

CRITICAL_ETA_DAYS = 5          # missing-critical + ETA within this many days => Critical
_SEV = {"READY": 0, "ATTENTION": 1, "NOT_READY": 2, "CRITICAL": 3}
_LABEL = {"READY": "Ready", "ATTENTION": "Attention Required",
          "CRITICAL": "Critical", "NOT_READY": "Not Ready"}


# Trailing voyage token: " V" / " V." / " V106509" / " Voy 12" / " Voyage 5".
# Strips voyage suffixes (e.g. V 12, VOYAGE 5) but preserves single letter suffixes (e.g. EFFIE V vs EFFIE).
_VOY_RE = re.compile(r"\s+(?:VOYAGE|VOY)(?![A-Z]).*$|\s+V(?:[\s\.-]*\d+.*)$", re.IGNORECASE)


def norm_vessel(name):
    """Canonical key for grouping free-text vessel names (no vessels master).
    Case-insensitive; ignores dots & extra spaces; drops a trailing voyage token,
    preserving standalone suffixes like 'EFFIE V' while collapsing 'EFFIE V.' to 'EFFIE V'."""
    if not name:
        return None
    s = str(name).upper().split(",")[0]   # drop trailing voyage/date after a comma
    s = s.replace(".", " ")               # dots -> space, so "V." == "V"
    s = re.sub(r"\s+", " ", s).strip()
    s = _VOY_RE.sub("", s).strip()        # strip a trailing voyage token (+ optional code)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _eager(q):
    # Shipment.lc is many-to-one (one row per shipment) so joinedload costs nothing extra;
    # everything below it is one-to-many and previously used joinedload too, which multiplies
    # the joined row count by the product of every collection's size per shipment. selectinload
    # issues one extra "WHERE parent_id IN (...)" query per relationship instead, with no fan-out.
    return q.options(
        joinedload(Shipment.lc).selectinload(LCMaster.products),
        selectinload(Shipment.commercial_invoices),
        selectinload(Shipment.bill_of_ladings),
        selectinload(Shipment.packing_lists),
        selectinload(Shipment.goods_declarations),
        selectinload(Shipment.financial_instruments),
        selectinload(Shipment.insurance_certificates),
    )


def _ship_qty(s):
    net, _src = resolve_net_weight_mt(s)
    return net if net is not None else 0.0


def _ship_amount(s):
    inv = s.commercial_invoices[0] if s.commercial_invoices else None
    cur = (inv.currency if inv and inv.currency else None) or (s.lc.currency if s.lc else None) or "USD"
    if inv and inv.total_amount_usd is not None:
        return float(inv.total_amount_usd), cur
    return None, cur


def _ship_packages(s):
    """Best-available package/coil count for the shipment using CI -> PL -> BL -> DPL -> SHP -> LC waterfall."""
    coils, _src = resolve_coils(s)
    return coils


def _ship_rate(s, amount, qty):
    """USD per MT — the invoice unit price, else derived amount / qty."""
    inv = s.commercial_invoices[0] if s.commercial_invoices else None
    if inv and inv.unit_price_usd is not None:
        return float(inv.unit_price_usd)
    if amount is not None and qty:
        return round(amount / qty, 2)
    return None


def item_type(lc):
    prods = lc.products if lc else []
    if not prods:
        return "(Unassigned)"
    # Prefer the normalized short item code; fall back to legacy product_code, then name.
    codes = {(p.item_code or p.product_code) for p in prods if (p.item_code or p.product_code)}
    if len(codes) <= 1:
        p = prods[0]
        return p.item_code or p.product_code or p.product_name or "(Unknown)"
    return "Mixed (multi-product)"


def _doc_status(s):
    """Return (present_dict, missing_labels, readiness_status) for one shipment."""
    present = {key: bool(getattr(s, rel)) for key, _, rel, _ in DOC_TYPES}
    missing = [(label, crit) for key, label, rel, crit in DOC_TYPES if not present[key]]
    missing_labels = [l for l, _ in missing]

    eta_days = (s.eta - date.today()).days if s.eta else None
    if not any(present.values()):
        status = "NOT_READY"
    else:
        crit_missing = any(c for _, c in missing)
        if crit_missing and eta_days is not None and eta_days <= CRITICAL_ETA_DAYS:
            status = "CRITICAL"
        elif missing:
            status = "ATTENTION"
        else:
            status = "READY"
    return present, missing_labels, status


def _doc_label(status):
    return {"READY": "Complete", "ATTENTION": "Pending",
            "CRITICAL": "Critical", "NOT_READY": "Critical"}.get(status, "Pending")


def _d(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def list_vessels(db: Session, upcoming_only: bool) -> dict:
    """Picker source: distinct (normalised) vessel names that have shipments,
    with shipment count and nearest ETA. Ordered by soonest ETA first."""
    rows = db.query(Shipment).filter(
        Shipment.vessel_name.isnot(None), Shipment.is_deleted.is_(False)).all()
    today = date.today()
    agg: dict[str, dict[str, Any]] = {}
    for s in rows:
        key = norm_vessel(s.vessel_name)
        if not key:
            continue
        if upcoming_only and (s.eta is None or s.eta < today):
            continue
        # Show the clean normalised name in the dropdown (one option per real vessel).
        a = agg.setdefault(key, {"vessel": key, "display": key,
                                 "shipment_count": 0, "next_eta": None})
        a["shipment_count"] += 1
        if s.eta and (a["next_eta"] is None or s.eta.isoformat() < a["next_eta"]):
            a["next_eta"] = s.eta.isoformat()
    items = list(agg.values())
    items.sort(key=lambda x: (x["next_eta"] is None, x["next_eta"] or "9999"))
    return {"count": len(items), "items": items}


def bulk_update_vessel(db: Session, data: VesselBulkUpdate, username: str) -> dict:
    """Bulk-update ETA, port status, and milestone dates for EVERY shipment of one
    normalised vessel. Raises ValueError (translated to HTTPException(400) by the
    router) for the same validation failures the original hand-checked."""
    vessel = str(data.vessel or "").strip()
    if not vessel:
        raise ValueError("vessel is required")
    target = norm_vessel(vessel)

    def _parse_date(raw: Optional[str], field: str):
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            raise ValueError(f"{field} must be YYYY-MM-DD")

    new_eta = _parse_date(data.eta, "eta")
    on_port_date = _parse_date(data.on_port_date, "on_port_date")
    departure_date = _parse_date(data.departure_date, "departure_date")
    port_status = data.port_status
    port_status = str(port_status).strip() if port_status is not None else None
    berth = str(data.berth or "").strip() or None

    if new_eta is None and not port_status and on_port_date is None and departure_date is None:
        raise ValueError("Provide eta, port_status, on_port_date, and/or departure_date to update.")

    core = (target or "").split(" ")[0]
    q = (
        db.query(Shipment)
        .options(selectinload(Shipment.bill_of_ladings))
        .filter(Shipment.is_deleted.is_(False))
    )
    if core:
        q = q.filter(Shipment.vessel_name.ilike(f"%{core}%"))
    ships = [s for s in q.all() if norm_vessel(s.vessel_name) == target]
    ship_ids = [s.shipment_id for s in ships]

    for s in ships:
        if new_eta is not None:
            s.eta = new_eta
            # A human explicitly set this ETA — locks it against the auto-ETA formula until
            # reset (see Shipment.eta_source).
            s.eta_source = "MANUAL"
        if port_status:
            apply_port_status(s, port_status, berth=berth, on_port_date=on_port_date, departure_date=departure_date)
            s.vessel_status_source = "MANUAL"
            s.vessel_status_updated_at = datetime.utcnow()
        else:
            if berth:
                s.kpt_berth = berth
            if on_port_date is not None:
                s.on_port_date = on_port_date
            if departure_date is not None:
                s.departure_date = departure_date

    bls_updated = 0
    if departure_date is not None and ship_ids:
        bls_updated = bulk_set_demurrage_start_for_shipments(db, ship_ids, departure_date)

    db.commit()
    logger.info(f"Vessel bulk-update '{target}': {len(ships)} shipment(s) "
                f"eta={new_eta} port_status={port_status} on_port={on_port_date} "
                f"departure={departure_date} demurrage_bls={bls_updated} by {username}")
    return {
        "success": True, "vessel": target, "updated": len(ships),
        "demurrage_bls_updated": bls_updated,
        "eta": new_eta.isoformat() if new_eta else None,
        "port_status": port_status,
        "on_port_date": on_port_date.isoformat() if on_port_date else None,
        "departure_date": departure_date.isoformat() if departure_date else None,
        "berth": berth,
        "status_options": list(STANDARD_STATUSES),
    }


def vessel_report(db: Session, vessel: str, eta_from: str, eta_to: str, port: str) -> dict:
    """Full vessel summary: KPIs, readiness, LC-wise rows, item-type / booked-by breakups,
    amount summary, missing-documents, and legacy (no-shipment) LCs."""
    target = norm_vessel(vessel)
    # Coarse SQL prefilter (first word of the vessel) to avoid scanning every shipment on
    # prod, then exact match on the normalised key in Python.
    core = (target or "").split(" ")[0]
    q = _eager(db.query(Shipment)).filter(Shipment.is_deleted.is_(False))
    if core:
        q = q.filter(Shipment.vessel_name.ilike(f"%{core}%"))
    ships = [s for s in q.all() if norm_vessel(s.vessel_name) == target]

    f_from, f_to = _d(eta_from), _d(eta_to)
    if f_from:
        ships = [s for s in ships if s.eta and s.eta >= f_from]
    if f_to:
        ships = [s for s in ships if s.eta and s.eta <= f_to]
    if port:
        pl = port.strip().lower()
        ships = [s for s in ships if (s.port_of_discharge or "").lower().find(pl) >= 0]

    display = next((s.vessel_name.strip() for s in ships if s.vessel_name), target)
    etas = sorted([s.eta for s in ships if s.eta])
    departures = sorted([s.departure_date for s in ships if s.departure_date])
    ports = Counter([s.port_of_discharge for s in ships if s.port_of_discharge])

    lc_wise, missing_docs = [], []
    item_agg = defaultdict(lambda: {"qty": 0.0, "lcs": set(), "shipments": 0})
    booked_agg = defaultdict(lambda: {"qty": 0.0, "amount": defaultdict(float), "lcs": set(), "shipments": 0})
    amount_rows = []
    value_by_cur = defaultdict(float)
    total_qty = 0.0
    total_bls = 0
    lc_ids = set()
    statuses = []
    missing_counter = Counter()

    for s in ships:
        lc = s.lc
        lc_no = lc.lc_number if lc else None
        if lc:
            lc_ids.add(lc.lc_id)
        bl = s.bill_of_ladings[0] if s.bill_of_ladings else None
        gd = s.goods_declarations[0] if s.goods_declarations else None
        if s.bill_of_ladings:
            total_bls += len(s.bill_of_ladings)
        qty = _ship_qty(s)
        total_qty += qty
        amount, cur = _ship_amount(s)
        # gd_number is null (not "") when the GD is not filed, so the UI shows "Not filed".
        gd_number = (gd.gd_number or None) if gd else None
        # "company" = the importing entity. No short-code (PCL/MEL/MAX) field exists in the
        # schema, so we surface the full importer name (lc, then GD). See report notes.
        company = (lc.importer_name if lc and lc.importer_name else None) or \
                  (gd.importer_name if gd and gd.importer_name else None)
        rate = _ship_rate(s, amount, qty)
        packages = _ship_packages(s)
        # LME rate (first non-null product current_lme) + container count across the LC's products.
        lme_rate, containers_manual = None, 0
        if lc:
            for p in lc.products:
                if lme_rate is None and p.current_lme is not None:
                    lme_rate = float(p.current_lme)
                containers_manual += int(p.num_containers or 0)
        # Container id text — BL -> DPL -> shipment remarks; count of listed containers when
        # found, else the manual per-product count entered on the LC.
        container_text, container_src = resolve_container_numbers(s)
        if container_text:
            tokens = [t for t in re.split(r"[,\n;/]+", container_text) if t.strip()]
            containers = len(tokens) if tokens else 1
        else:
            containers = containers_manual or None
        if amount is not None:
            value_by_cur[cur] += amount
        item_desc, item_src = resolve_item_description(s)
        itype = item_desc or item_type(lc)
        booked = (lc.booked_by if lc and lc.booked_by else None) or "(Unassigned)"
        supplier = (lc.supplier_name if lc else None) or "—"

        present, missing_labels, status = _doc_status(s)
        statuses.append(status)
        for ml in missing_labels:
            missing_counter[ml] += 1

        # Live vessel status (KPT-resolved when available, falling back to the static
        # shipment status) — not the raw static shipment.status alone, so "Current Status"
        # reflects the latest scrape rather than going stale between milestone updates.
        vs = resolve_vessel_status(s)

        lc_wise.append({
            "shipment_id": s.shipment_id, "lc_id": (lc.lc_id if lc else None),
            "gd_id": (gd.gd_id if gd else None),
            "lot_number": s.lot_number,
            "lc_number": lc_no, "supplier": supplier,
            "bl_number": bl.bl_number if bl else None,
            "gd_number": gd_number, "company": company,
            "hoa": (lc.hoa if lc else None),
            "bank": (lc.bank_name if lc else None),
            "payment_terms": (lc.payment_terms if lc else None),
            "vessel_name": s.vessel_name,
            "lme_rate": lme_rate, "containers": containers or None,
            "container_numbers": container_text,
            "qty_mt": qty, "packages": packages, "rate": rate,
            "amount": amount, "currency": cur, "item_type": itype, "booked_by": booked,
            "shipment_status": s.status, "doc_status": _doc_label(status),
            "readiness": status, "eta": s.eta.isoformat() if s.eta else None,
            "eta_source": s.eta_source,
            # Live-resolved "Current Status" (KPT scrape when available) + who last set it.
            "current_status": vs.get("vessel_status") or s.status,
            "vessel_status_source": s.vessel_status_source,
            "vessel_status_updated_at": s.vessel_status_updated_at.isoformat() if s.vessel_status_updated_at else None,
            "on_port_date": s.on_port_date.isoformat() if s.on_port_date else None,
            "departure_date": s.departure_date.isoformat() if s.departure_date else None,
        })
        amount_rows.append({
            "lc_number": lc_no, "bl_number": bl.bl_number if bl else None,
            "qty_mt": qty, "amount": amount, "currency": cur,
        })
        missing_docs.append({
            "lc_number": lc_no, "lc_id": (lc.lc_id if lc else None),
            "shipment_id": s.shipment_id,
            "bl_number": bl.bl_number if bl else None,
            "missing": missing_labels, "doc_status": _doc_label(status),
        })

        it = item_agg[itype]
        it["qty"] += qty
        it["shipments"] += 1
        if lc:
            it["lcs"].add(lc.lc_id)
        bk = booked_agg[booked]
        bk["qty"] += qty
        bk["shipments"] += 1
        if amount is not None:
            bk["amount"][cur] += amount
        if lc:
            bk["lcs"].add(lc.lc_id)

    # overall readiness = worst per-shipment status + a summary sentence
    overall = max(statuses, key=lambda st: _SEV[st]) if statuses else "NOT_READY"
    parts = [f"{n} shipment{'s' if n > 1 else ''} missing {lbl}"
             for lbl, n in missing_counter.most_common()]
    if overall == "READY":
        sentence = "All shipments have their tracked documents."
    elif not ships:
        sentence = "No shipments found for this vessel."
    else:
        sentence = f"{_LABEL[overall]} — " + ("; ".join(parts) if parts else "review shipments before arrival") + "."

    item_type_breakup = sorted(
        [{"item_type": k, "qty_mt": round(v["qty"], 3), "lcs": len(v["lcs"]), "shipments": v["shipments"]}
         for k, v in item_agg.items()], key=lambda x: -x["qty_mt"])
    booked_by_breakup = sorted(
        [{"booked_by": k, "qty_mt": round(v["qty"], 3), "amount": dict(v["amount"]),
          "lcs": len(v["lcs"]), "shipments": v["shipments"]}
         for k, v in booked_agg.items()], key=lambda x: -x["qty_mt"])

    # legacy LCs: vessel on lc_master, but no shipment exists for that LC
    legacy = []
    lcq = db.query(LCMaster).options(joinedload(LCMaster.products), joinedload(LCMaster.shipments)) \
            .filter(LCMaster.vessel_name.isnot(None))
    if core:
        lcq = lcq.filter(LCMaster.vessel_name.ilike(f"%{core}%"))
    for lc in lcq.all():
        if norm_vessel(lc.vessel_name) != target or lc.shipments:
            continue
        qty = sum(float(p.quantity) for p in lc.products if p.quantity is not None)
        prod0 = lc.products[0] if lc.products else None
        legacy.append({
            "lc_number": lc.lc_number, "lc_id": lc.lc_id, "supplier": lc.supplier_name or "—",
            "bl_number": lc.bl_number, "eta": lc.eta.isoformat() if lc.eta else None,
            "booked_qty_mt": round(qty, 3), "item_type": item_type(lc),
            "booked_by": lc.booked_by or "(Unassigned)",
            "gd_number": None, "company": lc.importer_name,
            "rate": float(prod0.lc_unit_price) if (prod0 and prod0.lc_unit_price is not None) else None,
            "packages": sum(int(p.pkgs_coils) for p in lc.products if p.pkgs_coils) or None,
        })

    shipments_needing_attention = sum(1 for st in statuses if st != "READY")
    return {
        "vessel": display,
        "eta_earliest": etas[0].isoformat() if etas else None,
        "eta_latest": etas[-1].isoformat() if etas else None,
        "departure_earliest": departures[0].isoformat() if departures else None,
        "departure_latest": departures[-1].isoformat() if departures else None,
        "port": (ports.most_common(1)[0][0] if ports else None),
        "kpis": {
            "total_lcs": len(lc_ids),
            "total_shipments": len(ships),
            "total_bls": total_bls,
            "total_qty_mt": round(total_qty, 3),
            "value_by_currency": dict(value_by_cur),
            "shipments_needing_attention": shipments_needing_attention,
        },
        "readiness": {"status": overall, "label": _LABEL[overall], "sentence": sentence},
        "lc_wise": lc_wise,
        "item_type_breakup": item_type_breakup,
        "booked_by_breakup": booked_by_breakup,
        "amount_rows": amount_rows,
        "value_by_currency": dict(value_by_cur),
        "missing_docs": missing_docs,
        "legacy_lcs": legacy,
    }


def bank_report(db: Session, date_from: str, date_to: str, bank: Optional[list[str]], lc_number: str,
                company: Optional[list[str]], item: Optional[list[str]], booked_by: Optional[list[str]],
                payment_term: str | None = None, indentor: Optional[list[str]] = None) -> dict:
    """Bank-Wise Report + Custom LC Report. LCs grouped by normalised issuing bank (with
    per-bank LC list, quantity & LC-amount totals + grand total) plus a flat LC-level list
    for the Custom LC Report. Filters: lc_date range, bank, LC number, company, item, booked_by, payment_term, indentor."""
    q = db.query(LCMaster).options(selectinload(LCMaster.products))
    q = q.filter((LCMaster.status.is_(None)) | (LCMaster.status != "CANCELLED"))

    df, dt = _d(date_from), _d(date_to)
    if df:
        q = q.filter(LCMaster.lc_date >= df)
    if dt:
        q = q.filter(LCMaster.lc_date <= dt)
    if lc_number:
        q = q.filter(LCMaster.lc_number.ilike(f"%{lc_number.strip()}%"))

    banks = {}
    bank_set, company_set, item_set, booked_set, indentor_set, tenor_set = set(), set(), set(), set(), set(), set()
    custom_rows = []
    bank_filter = set(_normalize_list(bank)) if bank else None
    company_filter = set(_normalize_list(company)) if company else None
    item_filter = set(_normalize_list(item)) if item else None
    booked_by_filter = set(_normalize_list(booked_by)) if booked_by else None
    indentor_filter = set(_normalize_list(indentor)) if indentor else None
    for lc in q.all():
        canon = norm_bank(lc.bank_name)
        itype = item_type(lc)
        company_val = (lc.importer_name or "").strip() or None   # our importing company
        booked_val = (lc.booked_by or "").strip() or None        # buyer / booked-by
        indentor_val = (lc.indentor or "").strip() or None
        tenor_val = da_sight(lc.payment_terms)
        # Collect dropdown options from ALL date-filtered LCs (before the select filters).
        bank_set.add(canon)
        if company_val:
            company_set.add(company_val)
        item_set.add(itype)
        if booked_val:
            booked_set.add(booked_val)
        if indentor_val:
            indentor_set.add(indentor_val)
        if tenor_val:
            tenor_set.add(tenor_val)

        # Apply the select filters (exact match against the collected options).
        if bank_filter and canon not in bank_filter:
            continue
        if company_filter and company_val not in company_filter:
            continue
        if item_filter and itype not in item_filter:
            continue
        if booked_by_filter and booked_val not in booked_by_filter:
            continue
        if payment_term and tenor_val != payment_term:
            continue
        if indentor_filter and indentor_val not in indentor_filter:
            continue

        qty = sum(float(p.quantity) for p in lc.products if p.quantity is not None)
        amt = sum(float(p.lc_amount) for p in lc.products if p.lc_amount is not None)
        if not amt:   # fallback: quantity * unit price
            amt = sum(float(p.quantity or 0) * float(p.lc_unit_price or 0) for p in lc.products)
        # LC rate = unit price (first product carrying one), else derived amount / qty.
        rate = next((float(p.lc_unit_price) for p in lc.products if p.lc_unit_price is not None), None)
        if rate is None and qty:
            rate = round(amt / qty, 2) if amt else None
        # Currency is stored inconsistently ("$ USD" vs "USD") — normalise to letters only.
        cur = re.sub(r"[^A-Z]", "", (lc.currency or "USD").upper()) or "USD"

        b = banks.setdefault(canon, {"bank": canon, "lcs": [], "total_qty": 0.0,
                                     "by_currency": defaultdict(float), "lc_count": 0})
        b["lcs"].append({
            "lc_id": lc.lc_id, "lc_number": lc.lc_number,
            "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
            "raw_bank": lc.bank_name, "bank": canon,
            "item": itype, "booked_by": booked_val, "company": company_val,
            "qty_mt": round(qty, 3), "amount": round(amt, 2), "currency": cur,
        })
        b["total_qty"] += qty
        b["by_currency"][cur] += amt
        b["lc_count"] += 1

        # Custom LC Report — flat LC-level row.
        custom_rows.append({
            "lc_id": lc.lc_id, "lc_number": lc.lc_number,
            "da_sight": tenor_val, "payment_terms": lc.payment_terms,
            "importer": company_val, "indentor": indentor_val,
            "bank": canon, "item": itype,
            "order_qty": round(qty, 3), "rate": rate,
            "amount": round(amt, 2), "currency": cur,
            "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
            "last_ship_date": lc.last_ship_date.isoformat() if lc.last_ship_date else None,  # L.S.D
        })

    bank_list = []
    grand_qty = 0.0
    grand_cur = defaultdict(float)
    grand_amt = 0.0
    for canon, b in banks.items():
        b["lcs"].sort(key=lambda x: x["lc_date"] or "", reverse=True)
        amt_all = sum(b["by_currency"].values())
        bank_list.append({
            "bank": canon, "lc_count": b["lc_count"],
            "total_qty": round(b["total_qty"], 3),
            "total_by_currency": {k: round(v, 2) for k, v in b["by_currency"].items()},
            "total_amount": round(amt_all, 2),   # numeric sum for the pie (mostly one currency)
            "lcs": b["lcs"],
        })
        grand_qty += b["total_qty"]
        for k, v in b["by_currency"].items():
            grand_cur[k] += v
        grand_amt += amt_all
    bank_list.sort(key=lambda x: -x["total_amount"])
    custom_rows.sort(key=lambda x: x["lc_date"] or "", reverse=True)

    return {
        "filters": {"date_from": date_from, "date_to": date_to, "bank": bank,
                    "lc_number": lc_number, "company": company, "item": item, "booked_by": booked_by,
                    "payment_term": payment_term, "indentor": indentor},
        "bank_options": sorted(bank_set),
        "company_options": sorted(company_set),
        "item_options": sorted(item_set),
        "booked_by_options": sorted(booked_set),
        "indentor_options": sorted(indentor_set),
        "payment_term_options": sorted(tenor_set),
        "banks": bank_list,
        "custom_lc_rows": custom_rows,
        "grand_total_qty": round(grand_qty, 3),
        "grand_total_by_currency": {k: round(v, 2) for k, v in grand_cur.items()},
        "grand_total_amount": round(grand_amt, 2),
    }


# ---------------------------------------------------------------------------
# Buyer / Booked-By Report (structured allocation aware)
# ---------------------------------------------------------------------------

def _lc_cur(lc):
    """LC currency, normalised to letters ('$ USD' -> 'USD')."""
    return re.sub(r"[^A-Z]", "", (lc.currency or "USD").upper()) or "USD"


def _lc_totals(lc):
    """(qty_mt, amount) for an LC from its product lines (amount falls back to qty*price)."""
    qty = sum(float(p.quantity) for p in lc.products if p.quantity is not None)
    amt = sum(float(p.lc_amount) for p in lc.products if p.lc_amount is not None)
    if not amt:
        amt = sum(float(p.quantity or 0) * float(p.lc_unit_price or 0) for p in lc.products)
    return qty, amt


def _buyer_rows_for_lc(lc):
    """Buyer split for one LC as [(buyer_name, qty_mt, amount), ...].

    Uses the structured allocation when present; otherwise falls back to the legacy
    free-text booked_by (the whole LC to that one name), so old records still appear."""
    if lc.buyer_allocation_type and lc.buyer_allocations:
        return [(a.buyer_name or "(Unassigned)",
                 float(a.quantity) if a.quantity is not None else 0.0,
                 float(a.amount) if a.amount is not None else 0.0)
                for a in lc.buyer_allocations]
    qty, amt = _lc_totals(lc)
    return [((lc.booked_by or "").strip() or "(Unassigned)", qty, amt)]


def buyer_report(db: Session, date_from: str, date_to: str, buyer: str) -> dict:
    """Buyer / Booked-By wise report. Splits each LC across its buyers using the structured
    allocation (lc_buyer_allocations); LCs without a structured allocation fall back to the
    legacy booked_by text so nothing is lost. Returns buyer-wise qty/amount/LC-count plus
    breakdowns by item, vessel, bank and company. Filters: lc_date range + optional buyer."""
    # Two independent one-to-many collections on the same parent — joinedload here would
    # join both in one query and multiply rows by products x allocations per LC.
    q = db.query(LCMaster).options(selectinload(LCMaster.products),
                                   selectinload(LCMaster.buyer_allocations))
    q = q.filter((LCMaster.status.is_(None)) | (LCMaster.status != "CANCELLED"))

    df, dt = _d(date_from), _d(date_to)
    if df:
        q = q.filter(LCMaster.lc_date >= df)
    if dt:
        q = q.filter(LCMaster.lc_date <= dt)

    # buyer -> {qty, lcs:set, amount:{cur: val}}
    buyers = defaultdict(lambda: {"qty": 0.0, "lcs": set(), "amount": defaultdict(float)})
    # (buyer, dim, currency) -> {qty, amount}
    by_item = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    by_vessel = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    by_bank = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    by_company = defaultdict(lambda: {"qty": 0.0, "amount": 0.0})
    buyer_set = set()
    grand_qty = 0.0
    grand_amt = defaultdict(float)
    grand_lcs = set()

    for lc in q.all():
        cur = _lc_cur(lc)
        itype = item_type(lc)
        vessel = (lc.vessel_name or "").strip() or "(No vessel)"
        bank = norm_bank(lc.bank_name)
        company = (lc.importer_name or "").strip() or "(Unknown)"
        for bname, bqty, bamt in _buyer_rows_for_lc(lc):
            buyer_set.add(bname)
            # apply the buyer filter after collecting options
            if buyer and bname != buyer:
                continue
            b = buyers[bname]
            b["qty"] += bqty
            b["lcs"].add(lc.lc_id)
            b["amount"][cur] += bamt
            by_item[(bname, itype, cur)]["qty"] += bqty
            by_item[(bname, itype, cur)]["amount"] += bamt
            by_vessel[(bname, vessel, cur)]["qty"] += bqty
            by_vessel[(bname, vessel, cur)]["amount"] += bamt
            by_bank[(bname, bank, cur)]["qty"] += bqty
            by_bank[(bname, bank, cur)]["amount"] += bamt
            by_company[(bname, company, cur)]["qty"] += bqty
            by_company[(bname, company, cur)]["amount"] += bamt
            grand_qty += bqty
            grand_amt[cur] += bamt
            grand_lcs.add(lc.lc_id)

    buyers_out = sorted(
        [{"buyer": k, "qty_mt": round(v["qty"], 3), "lc_count": len(v["lcs"]),
          "amount_by_currency": {c: round(a, 2) for c, a in v["amount"].items()},
          "amount_total": round(sum(v["amount"].values()), 2)}
         for k, v in buyers.items()],
        key=lambda x: -x["qty_mt"])

    def _combo(d, dim_key):
        return sorted(
            [{"buyer": k[0], dim_key: k[1], "currency": k[2],
              "qty_mt": round(val["qty"], 3), "amount": round(val["amount"], 2)}
             for k, val in d.items()],
            key=lambda x: (x["buyer"], -x["qty_mt"]))

    return {
        "filters": {"date_from": date_from, "date_to": date_to, "buyer": buyer},
        "buyer_options": sorted(buyer_set),
        "buyers": buyers_out,
        "by_item": _combo(by_item, "item"),
        "by_vessel": _combo(by_vessel, "vessel"),
        "by_bank": _combo(by_bank, "bank"),
        "by_company": _combo(by_company, "company"),
        "grand_total_qty": round(grand_qty, 3),
        "grand_total_by_currency": {c: round(a, 2) for c, a in grand_amt.items()},
        "grand_total_lc_count": len(grand_lcs),
    }


# ---------------------------------------------------------------------------
# Shipment-Wise Report — one row per shipment, full lifecycle milestone dates,
# with a selectable "sort/filter by" date field.
# ---------------------------------------------------------------------------

_SHIPMENT_DATE_FIELDS = (
    "eta", "ship_on_board", "bl_date", "invoice_date", "gd_date",
    "delivery_date", "payment_date", "fi_expiry", "lsd",
)


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _min_date(dates) -> Optional[date]:
    """Earliest non-null date — used when a shipment has several documents of the same
    kind (e.g. the soonest FI expiry). Matches legacy shipment_report.py's _min_date."""
    vals = [d for d in dates if d]
    return min(vals) if vals else None


def _shipment_wise_buyer(lc: Optional[LCMaster]) -> Optional[str]:
    """Structured buyer allocation if present, else the legacy free-text booked_by.
    Matches legacy shipment_report.py's _buyer()."""
    if not lc:
        return None
    names = [b.buyer_name for b in (lc.buyer_allocations or []) if b.buyer_name]
    if names:
        return ", ".join(dict.fromkeys(names))
    return lc.booked_by or None


def _shipment_wise_row(s: Shipment) -> dict:
    lc = s.lc
    base = shipment_summary(s)

    # Document-derived dates (earliest of each kind when several are attached), with a
    # fallback to the LC's own tracking fields for legacy LCs that carry these directly.
    # Matches legacy shipment_report.py::_row() — restored after the port to `ordered_docs()`
    # (primary/latest doc) silently changed the semantics to "most recent" instead of
    # "earliest".
    inv_date = _min_date([ci.invoice_date for ci in s.commercial_invoices]) or (lc.invoice_date if lc else None)
    pl_date = _min_date([pl.packing_date for pl in s.packing_lists])
    bl_date = s.bl_date or _min_date([bl.bl_date for bl in s.bill_of_ladings])
    fi_docs = list(s.financial_instruments or [])
    if lc and hasattr(lc, "financial_instruments") and lc.financial_instruments:
        fi_docs.extend(lc.financial_instruments)

    fi_dates = [f.expiry_date for f in fi_docs if f.expiry_date] or \
               [f.final_date_of_shipment for f in fi_docs if f.final_date_of_shipment] or \
               [f.created_at.date() for f in fi_docs if getattr(f, "created_at", None)]

    fi_expiry = _min_date(fi_dates) or (lc.expiry_date if lc else None) or (lc.latest_shipment_date if lc and hasattr(lc, "latest_shipment_date") else None)
    ins_date = _min_date([ic.issue_date for ic in s.insurance_certificates if ic.issue_date]) or \
               _min_date([ic.created_at.date() for ic in s.insurance_certificates if ic.created_at]) or \
               (getattr(lc, "insurance_date", None) if lc else None)
    gd_date = _min_date([g.filing_date for g in s.goods_declarations])
    eta_khi = s.eta or (lc.eta if lc else None)
    payment_date = s.payment_date or (lc.doc_payment_date if lc else None)
    exchange_rate = s.exchange_rate or (lc.exchange_rate if lc else None) or (lc.doc_payment_rate if lc else None)
    bank_intimation = s.intimation_date or (lc.bank_int_date if lc else None)
    delivery_date = s.delivery_date or (lc.delivery_date if lc else None)

    # "Shipment On Board" has no dedicated shipment column — etd's own comment documents
    # it as defaulting from the BL/shipped-on-board date; fall back to the LC's ship_on_board.
    ship_on_board = base["etd"] or (lc.ship_on_board.isoformat() if lc and lc.ship_on_board else None)
    metrics = report_weight_coils(s)
    return {
        **base,
        "buyer": _shipment_wise_buyer(lc),
        "head_of_account": lc.hoa if lc else None,
        "ship_on_board": ship_on_board,
        "eta_khi": _iso(eta_khi),
        "country_port": base["country_port"] or s.port_of_discharge,
        "fi_expiry": _iso(fi_expiry),
        "invoice_date": _iso(inv_date),
        "pl_date": _iso(pl_date),
        "bl_date": _iso(bl_date),
        # DPL weight/coils/transparency fields resolved via the CI -> PL -> BL -> DPL ->
        # shipment -> LC waterfall (see shipment_metrics.report_weight_coils).
        "dpl_weight_mt": metrics["dpl_weight_mt"],
        "dpl_coils": metrics["dpl_coils"],
        "container_numbers": metrics["container_numbers"],
        "weight_source": metrics["weight_source"],
        "coils_source": metrics["coils_source"],
        "using_dpl_fallback": metrics["using_dpl_fallback"],
        "insurance_date": _iso(ins_date),
        "gd_date": _iso(gd_date),
        "payment_date": _iso(payment_date),
        "exchange_rate": float(exchange_rate) if exchange_rate is not None else None,
        "bank_intimation": _iso(bank_intimation),
        "delivery_date": _iso(delivery_date),
        "lsd": lc.last_ship_date.isoformat() if lc and lc.last_ship_date else None,
    }


def _shipment_wise_options(db: Session) -> dict:
    """Distinct buyer/HOA/vessel/status values for the filter dropdowns, across every
    non-deleted shipment regardless of the report's own filters (so the dropdown always
    shows every possible value). Previously this re-ran a full duplicate ORM scan with a
    joinedload(Shipment.lc) on every single report request; these are now cheap
    SELECT-DISTINCT-column queries (no relationship loading), Redis-cached for 10 minutes
    since these values change rarely."""
    import orjson
    from core.redis import redis_cache

    cache_key = "lme:reports:shipment-wise:options"
    cached = redis_cache.get(cache_key)
    if cached:
        try:
            return orjson.loads(cached)
        except Exception:
            pass

    base = db.query(Shipment).join(LCMaster, Shipment.lc_id == LCMaster.lc_id, isouter=True) \
        .filter(Shipment.is_deleted.is_(False))
    buyers = {r[0] for r in base.with_entities(
        func.coalesce(LCMaster.booked_by, LCMaster.importer_name)).distinct() if r[0]}
    hoas = {r[0] for r in base.with_entities(LCMaster.hoa).distinct() if r[0]}
    v_raw = [norm_vessel(r[0]) for r in db.query(Shipment.vessel_name).filter(
        Shipment.is_deleted.is_(False), Shipment.vessel_name.isnot(None)).all()]
    vessels = {v for v in v_raw if v is not None}
    statuses = {r[0] for r in db.query(Shipment.status).filter(
        Shipment.is_deleted.is_(False), Shipment.status.isnot(None)).distinct()}

    result = {
        "buyers": sorted(buyers), "hoas": sorted(hoas),
        "vessels": sorted(vessels), "statuses": sorted(statuses),
    }
    try:
        redis_cache.set(cache_key, orjson.dumps(result).decode("utf-8"), ex=600)
    except Exception:
        pass
    return result


def shipment_wise_report(
    db: Session,
    date_field: str = "eta",
    date_from=None,
    date_to=None,
    buyer=None,
    hoa=None,
    vessel=None,
    status=None,
    lc_id: Optional[int] = None,
    q=None,
) -> dict:
    if date_field not in _SHIPMENT_DATE_FIELDS:
        date_field = "eta"

    buyer = _normalize_list(buyer)
    hoa = _normalize_list(hoa)
    vessel = _normalize_list(vessel)
    status = _normalize_list(status)

    query = db.query(Shipment).filter(Shipment.is_deleted.is_(False)).options(
        joinedload(Shipment.lc).selectinload(LCMaster.products),
        joinedload(Shipment.lc).selectinload(LCMaster.buyer_allocations),
        selectinload(Shipment.bill_of_ladings),
        selectinload(Shipment.commercial_invoices),
        selectinload(Shipment.packing_lists),
        selectinload(Shipment.goods_declarations),
        selectinload(Shipment.financial_instruments),
        selectinload(Shipment.insurance_certificates),
    )
    if lc_id:
        query = query.filter(Shipment.lc_id == lc_id)
    if status:
        query = query.filter(Shipment.status.in_(status))
    if vessel:
        query = query.filter(Shipment.vessel_name.in_(vessel))
    if q:
        term = f"%{q.upper()}%"
        query = query.filter(
            Shipment.shipment_ref.ilike(term)
            | Shipment.vessel_name.ilike(term)
            | Shipment.lot_number.ilike(term)
            | Shipment.lc.has(LCMaster.lc_number.ilike(term))
        )
    if hoa:
        query = query.filter(Shipment.lc.has(LCMaster.hoa.in_(hoa)))
    if buyer:
        query = query.filter(Shipment.lc.has(
            LCMaster.booked_by.in_(buyer) | LCMaster.importer_name.in_(buyer)
        ))

    rows = [_shipment_wise_row(s) for s in query.all()]

    if date_from:
        rows = [r for r in rows if r.get(date_field) and r[date_field] >= date_from]
    if date_to:
        rows = [r for r in rows if r.get(date_field) and r[date_field] <= date_to]

    rows.sort(key=lambda r: r.get(date_field) or "", reverse=True)

    lc_ids = {r["lc_id"] for r in rows if r.get("lc_id")}
    delivered_statuses = {"DELIVERED", "CLOSED"}
    delivered_count = sum(1 for r in rows if r.get("status") in delivered_statuses)

    options = _shipment_wise_options(db)

    return {
        "date_field": date_field,
        "options": options,
        "rows": rows,
        "totals": {
            "shipment_count": len(rows),
            "lc_count": len(lc_ids),
            "delivered_count": delivered_count,
            "in_transit_count": len(rows) - delivered_count,
            # Resolved via the CI -> PL -> BL -> DPL -> shipment -> LC waterfall (dpl_weight_mt/
            # dpl_coils), NOT the raw shipment-table columns — those are frequently null and
            # would silently undercount (matches legacy shipment_report.py's totals).
            "total_gross_weight_mt": round(sum(float(r["dpl_weight_mt"]) for r in rows
                                                if r.get("dpl_weight_mt") is not None), 3),
            "total_coils": sum(int(r["dpl_coils"]) for r in rows if r.get("dpl_coils") is not None) or None,
        },
    }


# ---------------------------------------------------------------------------
# Main Report — the full shipment ledger, one row per shipment, the same
# figures as the Shipment Hub list, formatted for print/export.
# ---------------------------------------------------------------------------

def _main_report_party_name(r: dict):
    return (r.get("party_name") or r.get("booked_by") or "").strip() or None


def normalize_vessel_name(name: str | None) -> str:
    if not name:
        return ""
    import re
    val = name.strip().upper()
    val = re.sub(r'[\.,\s]+$', '', val)  # strip trailing period, comma, space
    val = re.sub(r'\s+', ' ', val)       # replace multiple spaces with single space
    return val


def normalize_bank_name(name: str | None) -> str:
    if not name:
        return ""
    import re
    val = name.strip()
    val_upper = val.upper()
    
    if "AL BARAKA" in val_upper or "ALBARAKA" in val_upper:
        return "Al Baraka"
    if "ASKARI" in val_upper:
        return "Askari Bank"
    if "ALFALAH" in val_upper or "BAFL" in val_upper:
        return "Bank Alfalah"
    if "HABIB METRO" in val_upper or "HMBL" in val_upper or "HABIB METROPOLITAN" in val_upper:
        return "Habib Metropolitan Bank"
    if "AL HABIB" in val_upper or "BAHL" in val_upper:
        return "Bank AL Habib"
    if "ISLAMI" in val_upper:
        return "BankIslami"
    if "MOBILINK" in val_upper or "MMBL" in val_upper:
        return "Mobilink Microfinance Bank"
    if "SONERI" in val_upper:
        return "Soneri Bank"
    if "UNITED BANK" in val_upper or "UBL" in val_upper:
        return "United Bank Limited"
    if "MCB" in val_upper:
        return "MCB Bank"
    if "HABIB BANK" in val_upper or "HBL" in val_upper:
        return "Habib Bank Limited"
    if "MEEZAN" in val_upper:
        return "Meezan Bank"
    if "NATIONAL BANK" in val_upper or "NBP" in val_upper:
        return "National Bank of Pakistan"
    
    val = re.sub(r'[\.,\s]+$', '', val)
    val = re.sub(r'\s+', ' ', val)
    return val


def main_report(
    db: Session,
    search=None,
    status=None,
    vessel=None,
    eta_from=None,
    eta_to=None,
    importer=None,
    item_type=None,
    party=None,
    bank=None,
) -> dict:
    status = _normalize_list(status)
    vessel = _normalize_list(vessel)
    importer = _normalize_list(importer)
    item_type = _normalize_list(item_type)
    party = _normalize_list(party)
    bank = _normalize_list(bank)

    query = db.query(Shipment).filter(Shipment.is_deleted.is_(False)).options(
        joinedload(Shipment.lc).selectinload(LCMaster.products),
        selectinload(Shipment.bill_of_ladings),
        selectinload(Shipment.commercial_invoices),
        selectinload(Shipment.packing_lists),
        selectinload(Shipment.goods_declarations),
        selectinload(Shipment.financial_instruments),
        # extra_documents (DPL detection via has_dpl()) was missing here — every row's
        # weight/coil/container resolution was lazy-loading it individually (N+1).
        selectinload(Shipment.extra_documents),
    )
    if status:
        query = query.filter(Shipment.status.in_(status))
    if vessel:
        query = query.filter(or_(*[Shipment.vessel_name.ilike(f"%{v}%") for v in vessel]))
    if eta_from:
        query = query.filter(Shipment.eta >= eta_from)
    if eta_to:
        query = query.filter(Shipment.eta <= eta_to)
    if search:
        term = f"%{search.upper()}%"
        query = query.filter(
            Shipment.shipment_ref.ilike(term)
            | Shipment.vessel_name.ilike(term)
            | Shipment.lc.has(LCMaster.lc_number.ilike(term))
            | Shipment.lc.has(LCMaster.importer_name.ilike(term))
        )

    shipments = query.order_by(Shipment.eta.desc().nullslast()).all()
    resolver = company_resolver(db)
    all_rows = []
    for s in shipments:
        base = shipment_summary(s)
        lc = s.lc
        row = {
            **base,
            "hoa": lc.hoa if lc else None,
            "importer_name": lc.importer_name if lc else None,
            "booked_by": (lc.booked_by if lc else None) or (lc.importer_name if lc else None),
        }
        # Normalize bank and vessel names to prevent repetitions in reports/filters
        if row.get("bank_name"):
            row["bank_name"] = normalize_bank_name(row["bank_name"])
        if row.get("vessel_name"):
            row["vessel_name"] = normalize_vessel_name(row["vessel_name"])
            
        enrich_company_fields(row, resolver, field="importer_name")
        all_rows.append(row)

    # Build dropdown options from ALL active shipments so filtering never shrinks available dropdown choices
    base_shipments = db.query(Shipment).filter(Shipment.is_deleted.is_(False)).options(
        joinedload(Shipment.lc).selectinload(LCMaster.products)
    ).all()
    
    options_statuses = set()
    options_vessels = set()
    options_importers = set()
    options_banks = set()
    options_item_types = set()
    options_parties = set()
    
    for bs in base_shipments:
        if bs.status:
            options_statuses.add(str(bs.status))
        if bs.vessel_name:
            options_vessels.add(str(normalize_vessel_name(bs.vessel_name)))
        if bs.lc:
            imp_res = resolver.resolve(bs.lc.importer_name or "")
            code = imp_res.get("short_code") or bs.lc.importer_name
            if code:
                options_importers.add(str(code))
            if bs.lc.bank_name:
                options_banks.add(str(normalize_bank_name(bs.lc.bank_name)))
            for prod in (bs.lc.products or []):
                if prod.product_code:
                    options_item_types.add(str(prod.product_code))
            party_name = (bs.lc.booked_by or bs.lc.importer_name or "").strip()
            if party_name:
                options_parties.add(party_name)
                
    options = {
        "statuses": sorted(options_statuses),
        "vessels": sorted(options_vessels),
        "importers": sorted(options_importers),
        "banks": sorted(options_banks),
        "item_types": sorted(options_item_types),
        "parties": sorted(options_parties),
    }

    rows = all_rows
    if importer:
        importer_set = set(importer)
        rows = [r for r in rows if (r.get("company_code") or "") in importer_set]
    if item_type:
        item_type_set = set(item_type)
        rows = [r for r in rows if (r.get("item_category") or "") in item_type_set]
    if party:
        party_set = set(party)
        rows = [r for r in rows if _main_report_party_name(r) in party_set]
    if bank:
        bank_set = set(bank)
        rows = [r for r in rows if (r.get("bank_name") or "") in bank_set]

    total_qty_kgs = sum(float(r.get("qty_kgs") or 0.0) for r in rows)
    total_qty_mt = round(sum(float(r["total_net_weight_mt"]) for r in rows
                             if r.get("total_net_weight_mt") is not None), 3)
    total_amount = round(sum(float(r["lc_amount"]) for r in rows if r.get("lc_amount") is not None), 2)

    return {
        "options": options,
        "rows": rows,
        "totals": {
            "shipment_count": len(rows),
            "total_qty_kgs": total_qty_kgs,
            "total_qty_mt": total_qty_mt,
            "total_amount": total_amount,
        },
    }


# ===========================================================================
# Pending Order Report — open/in-pipeline LCs grouped by item for buyer-wise tracking
# ===========================================================================

_PENDING_STATUSES = ("OPEN", "SHIPPED")


def _product_item(p: LCProduct) -> str:
    return (p.item_code or p.product_code or p.product_name or "(Unknown)").strip()


def _po_buyer_label(lc: LCMaster):
    if lc.booked_by and lc.booked_by.strip():
        return lc.booked_by.strip()
    names = [a.buyer_name.strip() for a in (lc.buyer_allocations or []) if a.buyer_name]
    if not names:
        return None
    return "+".join(names) if len(names) > 1 else names[0]


def _po_lc_eta(lc: LCMaster):
    etas = []
    for s in lc.shipments or []:
        if getattr(s, "is_deleted", False):
            continue
        if s.eta:
            etas.append(s.eta)
    if etas:
        return min(etas).isoformat()
    if lc.eta:
        return lc.eta.isoformat()
    return None


def _po_lc_vessel(lc: LCMaster):
    for s in lc.shipments or []:
        if getattr(s, "is_deleted", False):
            continue
        if s.vessel_name:
            return s.vessel_name.strip()
    return (lc.vessel_name or "").strip() or None


def _po_line_amount(p: LCProduct):
    if p.lc_amount is not None:
        return float(p.lc_amount)
    if p.quantity is not None and p.lc_unit_price is not None:
        return round(float(p.quantity) * float(p.lc_unit_price), 2)
    return None


def pending_order_report(
    db: Session,
    *,
    buyer: Optional[list[str]] = None,
    item: Optional[list[str]] = None,
    origin: Optional[list[str]] = None,
    importer: Optional[list[str]] = None,
    bank: Optional[list[str]] = None,
    search=None,
    lc_date_from=None,
    lc_date_to=None,
    eta_from=None,
    eta_to=None,
) -> dict:
    """Pending Order Report — open/in-pipeline LCs with item grouping (matches the manual
    upcoming-orders sheet: LC #, item, size, buyer, origin, qty, rate, dates, ETA)."""
    buyer = _normalize_list(buyer)
    item = _normalize_list(item)
    origin = _normalize_list(origin)
    importer = _normalize_list(importer)
    bank = _normalize_list(bank)

    q = (
        db.query(LCMaster)
        .options(
            joinedload(LCMaster.products),
            joinedload(LCMaster.buyer_allocations),
            joinedload(LCMaster.contract).selectinload(Contract.items),
            # selectinload (not joinedload) for shipments + their doc relations so the
            # resolve_net_weight_mt() waterfall below doesn't trigger N+1 lazy loads, without
            # the row fan-out joinedload would cause across these one-to-many collections.
            selectinload(LCMaster.shipments).selectinload(Shipment.commercial_invoices),
            selectinload(LCMaster.shipments).selectinload(Shipment.packing_lists),
            selectinload(LCMaster.shipments).selectinload(Shipment.bill_of_ladings),
            selectinload(LCMaster.shipments).selectinload(Shipment.extra_documents),
        )
        .filter(LCMaster.status.in_(_PENDING_STATUSES))
    )
    if lc_date_from:
        q = q.filter(LCMaster.lc_date >= lc_date_from)
    if lc_date_to:
        q = q.filter(LCMaster.lc_date <= lc_date_to)

    lcs = q.order_by(LCMaster.lc_date.desc(), LCMaster.lc_number).all()
    resolver = company_resolver(db)

    all_rows = []
    buyer_set, item_set, origin_set, importer_set, bank_set = set(), set(), set(), set(), set()

    for lc in lcs:
        buyer_label = _po_buyer_label(lc)
        importer_val = (lc.importer_name or "").strip() or None
        imp_res = resolver.resolve(importer_val)
        bank_val = norm_bank(lc.bank_name) if lc.bank_name else None
        eta_val = _po_lc_eta(lc)
        vessel_val = _po_lc_vessel(lc)

        if buyer_label:
            buyer_set.add(buyer_label)
        if importer_val:
            code = imp_res.get("short_code")
            if code:
                importer_set.add(code)
        if bank_val:
            bank_set.add(bank_val)

        # LC-level shipped quantity — live sum via the CI->PL->BL->DPL->SHP->LC waterfall,
        # not the stale, import-time-only lc_products.shipped_quantity column. Allocated
        # across this LC's product lines proportionally to each line's order quantity,
        # since shipments track quantity per-LC, not per-product-line.
        lc_shipped_mt = sum(
            (resolve_net_weight_mt(s)[0] or 0.0)
            for s in (lc.shipments or []) if not getattr(s, "is_deleted", False)
        ) or None
        lc_ordered_mt = sum(
            float(p.quantity) for p in (lc.products or []) if p.quantity is not None
        ) or None

        # Mill Name lives on the Contract's line items, not on the LC product line — LCs are
        # opened against a contract 1:1, so when the line counts match we correlate the two
        # lists positionally (by line_no / line_id order) to pull the contract-side value.
        # LCProduct.mill_name (if ever populated directly) is used as a fallback.
        products = lc.products or []
        contract_items = sorted(lc.contract.items, key=lambda ci: ci.line_no or 0) if lc.contract else []
        contract_mill_by_line = {}
        if contract_items and len(contract_items) == len(products):
            ordered_products = sorted(products, key=lambda pp: pp.line_id)
            for pp, ci in zip(ordered_products, contract_items):
                if ci.mill_name:
                    contract_mill_by_line[pp.line_id] = ci.mill_name

        for p in lc.products or []:
            item_val = _product_item(p)
            origin_val = (p.origin or "").strip() or None
            item_set.add(item_val)
            if origin_val:
                origin_set.add(origin_val)

            qty = float(p.quantity) if p.quantity is not None else None
            rate = float(p.lc_unit_price) if p.lc_unit_price is not None else None
            amount = _po_line_amount(p)
            cur = (lc.currency or "USD").strip() or "USD"

            shipped_qty_mt = None
            if lc_shipped_mt is not None and lc_ordered_mt and qty is not None:
                shipped_qty_mt = round(lc_shipped_mt * (qty / lc_ordered_mt), 3)
            balance_qty_mt = round(qty - (shipped_qty_mt or 0.0), 3) if qty is not None else None

            row = {
                "lc_id": lc.lc_id,
                "lc_number": lc.lc_number,
                "item": item_val,
                "size": (p.size or "").strip() or None,
                "grade": (p.grade or "").strip() or None,
                "mill_name": contract_mill_by_line.get(p.line_id) or (p.mill_name or "").strip() or None,
                "buyer": buyer_label,
                "origin": origin_val,
                "order_qty_mt": round(qty, 3) if qty is not None else None,
                "shipped_qty_mt": shipped_qty_mt,
                "balance_qty_mt": balance_qty_mt,
                "lc_rate": rate,
                "lc_amount": amount,
                "currency": cur,
                "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
                "last_ship_date": lc.last_ship_date.isoformat() if lc.last_ship_date else None,
                "eta": eta_val,
                "importer": importer_val,
                "company_code": imp_res.get("short_code"),
                "company_matched": imp_res.get("matched"),
                "bank": bank_val,
                "status": lc.status,
                "vessel": vessel_val,
                "payment_terms": lc.payment_terms,
            }
            all_rows.append(row)

    rows = all_rows
    # Pending Order Report is meant to show only unshipped/upcoming balances (per its own
    # "Open LCs with pending unshipped order balances" subtitle) — the OPEN/SHIPPED status
    # filter above isn't enough on its own, since a line can be fully (or over-)shipped
    # while its parent LC is still SHIPPED rather than CLOSED. Drop those settled lines;
    # keep lines with an unknown balance (qty not resolvable) rather than guess.
    rows = [r for r in rows if r.get("balance_qty_mt") is None or float(r["balance_qty_mt"]) > 0]
    if buyer:
        needles = [b.strip().lower() for b in buyer]
        rows = [r for r in rows if r.get("buyer") and any(n in str(r["buyer"]).lower() for n in needles)]

    if item:
        item_set = set(item)
        rows = [r for r in rows if r.get("item") in item_set]
    if origin:
        origin_set = set(origin)
        rows = [r for r in rows if (r.get("origin") or "") in origin_set]
    if importer:
        rows = [r for r in rows if any(matches_company_code(resolver, r.get("importer"), code) for code in importer)]
    if bank:
        bank_set = set(bank)
        rows = [r for r in rows if (r.get("bank") or "") in bank_set]

    if search:
        needle = search.strip().lower()
        rows = [
            r for r in rows
            if needle in " ".join(str(r.get(k) or "") for k in (
                "lc_number", "item", "buyer", "origin", "importer", "bank", "vessel", "size",
            )).lower()
        ]

    if eta_from or eta_to:
        kept = []
        for r in rows:
            raw = r.get("eta")
            if not raw:
                continue
            dd = date.fromisoformat(str(raw))
            if eta_from and dd < eta_from:
                continue
            if eta_to and dd > eta_to:
                continue
            kept.append(r)
        rows = kept

    # Group by item (preserve item sort order).
    groups_map = defaultdict(list)
    for r in rows:
        groups_map[str(r.get("item") or "Po Line")].append(r)

    groups = []
    for item_name in sorted(groups_map.keys()):
        grp_rows = groups_map[item_name]
        total_qty = sum(float(r["order_qty_mt"]) for r in grp_rows if r.get("order_qty_mt") is not None)
        # Weighted average rate where qty is known.
        w_num = sum(float(r["order_qty_mt"] or 0.0) * float(r["lc_rate"] or 0.0) for r in grp_rows if r.get("order_qty_mt"))
        w_den = sum(float(r["order_qty_mt"]) for r in grp_rows if r.get("order_qty_mt") and r.get("lc_rate") is not None)
        avg_rate = round(w_num / w_den, 2) if w_den else None
        groups.append({
            "item": item_name,
            "row_count": len(grp_rows),
            "total_qty_mt": round(total_qty, 3),
            "avg_rate": avg_rate,
            "rows": grp_rows,
        })

    total_qty = sum(float(r["order_qty_mt"]) for r in rows if r.get("order_qty_mt") is not None)
    total_amount = round(sum(float(r["lc_amount"]) for r in rows if r.get("lc_amount") is not None), 2)

    return {
        "today": date.today().isoformat(),
        "totals": {
            "lc_lines": len(rows),
            "total_qty_mt": round(total_qty, 3),
            "total_amount": total_amount,
            "item_groups": len(groups),
        },
        "groups": groups,
        "rows": rows,
        "options": {
            "buyers": sorted(buyer_set),
            "items": sorted(item_set),
            "origins": sorted(origin_set),
            "importers": sorted(importer_set),
            "banks": sorted(bank_set),
        },
    }
