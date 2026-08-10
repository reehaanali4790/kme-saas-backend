"""
Executive dashboard (Dashboard 2) — aggregated KPIs and chart data in one call.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    BillOfLading,
    Contract,
    EdbApproval,
    GoodsDeclaration,
    LCMaster,
    LCProduct,
    Shipment,
)
from modules.shipments.demurrage_service import compute_demurrage, get_or_create_config
from modules.shipments.container_detention_service import compute_container_detention
from modules.weboc.sro_service import compute_usage, approval_to_dict
from infrastructure.normalization.normalization_service import company_resolver


def _eta_status_label(shipment: Shipment, today: date) -> tuple[str, str]:
    """Return (label, color_token) for the ETA dashboard."""
    if shipment.delivery_date or (shipment.status or "").upper() == "DELIVERED":
        return "Arrived", "green"
    if shipment.on_port_date:
        return "On Port", "blue"
    if shipment.departure_date:
        return "In Transit", "yellow"
    if shipment.eta:
        days = (shipment.eta - today).days
        if days < 0:
            return "Overdue", "red"
        if days <= 3:
            return "On Schedule", "green"
        return "In Transit", "yellow"
    return "Booked", "blue"


def _shipment_bucket(status: str | None) -> str:
    st = (status or "").upper()
    if st in ("DELIVERED", "PAYMENT_MADE", "ACCEPTED", "CLOSED"):
        return "Arrived"
    if st in ("PENDING", "COPY_DOCS_RECEIVED"):
        return "Booked"
    return "In Transit"


def v2_summary(db: Session) -> dict:
    today = date.today()
    dem_cfg = get_or_create_config(db)
    resolver = company_resolver(db)

    total_shipments = (
        db.query(func.count(Shipment.shipment_id))
        .filter(Shipment.is_deleted.is_(False))
        .scalar()
        or 0
    )

    ships = (
        db.query(Shipment)
        .options(
            joinedload(Shipment.lc).joinedload(LCMaster.products),
            joinedload(Shipment.goods_declarations),
            joinedload(Shipment.bill_of_ladings),
        )
        .filter(Shipment.is_deleted.is_(False))
        .all()
    )

    total_qty_kgs = 0.0
    country_qty_mt: dict[str, float] = defaultdict(float)
    monthly_trend: dict[str, int] = defaultdict(int)
    item_qty_mt: dict[str, float] = defaultdict(float)
    supplier_agg: dict[str, dict] = defaultdict(lambda: {"qty_mt": 0.0, "short_mt": 0.0, "items": set()})
    shipment_buckets: dict[str, int] = defaultdict(int)

    for s in ships:
        qty_mt = float(s.total_net_weight_mt or s.delivered_quantity_mt or 0)
        total_qty_kgs += qty_mt * 1000

        if s.created_at:
            key = s.created_at.date().replace(day=1).isoformat()
            monthly_trend[key] += 1

        shipment_buckets[_shipment_bucket(s.status)] += 1

        lc = s.lc
        if lc and lc.products:
            prod = lc.products[0]
            origin = (prod.origin or "").strip()
            if origin and qty_mt:
                country_qty_mt[origin] += qty_mt
            item_key = (prod.item_code or prod.product_code or prod.product_name or "Other").strip()
            if qty_mt:
                item_qty_mt[item_key] += qty_mt

        if lc and lc.supplier_name:
            sup = lc.supplier_name.strip()
            supplier_agg[sup]["qty_mt"] += qty_mt
            expected = float(s.expected_quantity_mt or 0)
            if expected and qty_mt < expected:
                supplier_agg[sup]["short_mt"] += expected - qty_mt
            if lc.products:
                it = lc.products[0].item_code or lc.products[0].product_code
                if it:
                    supplier_agg[sup]["items"].add(it)

    total_containers = db.query(func.coalesce(func.sum(LCProduct.num_containers), 0)).scalar() or 0
    total_lcs = db.query(func.count(LCMaster.lc_id)).scalar() or 0
    open_lcs = (
        db.query(func.count(LCMaster.lc_id)).filter(LCMaster.status == "OPEN").scalar() or 0
    )
    lc_amount_usd = float(db.query(func.coalesce(func.sum(LCProduct.lc_amount), 0)).scalar() or 0)

    gd_count = db.query(func.count(GoodsDeclaration.gd_id)).scalar() or 0
    duty_total_pkr = float(
        db.query(func.coalesce(func.sum(GoodsDeclaration.total_duties_pkr), 0)).scalar() or 0
    )

    gd_type_counts = {
        (t or "UNKNOWN"): c
        for t, c in db.query(GoodsDeclaration.gd_type, func.count(GoodsDeclaration.gd_id))
        .group_by(GoodsDeclaration.gd_type)
        .all()
    }

    # ETA dashboard — upcoming + recent (same window as dashboard arrivals)
    grace = today - timedelta(days=7)
    eta_rows = []
    for s in ships:
        if not s.eta or s.eta < grace:
            continue
        if (s.status or "").upper() in ("PAYMENT_MADE", "ACCEPTED", "DELIVERED"):
            continue
        label, color = _eta_status_label(s, today)
        eta_rows.append({
            "eta": s.eta.isoformat(),
            "vessel": s.vessel_name,
            "status": label,
            "status_color": color,
            "days_to_eta": (s.eta - today).days,
            "shipment_id": s.shipment_id,
            "lc_number": s.lc.lc_number if s.lc else None,
        })
    eta_rows.sort(key=lambda r: (r["days_to_eta"] < 0, r["eta"]))

    # Bank exposure — open LC value by bank
    bank_map: dict[str, dict] = defaultdict(lambda: {"lc_amount": 0.0, "lc_count": 0})
    open_lcs_list = (
        db.query(LCMaster)
        .options(joinedload(LCMaster.products))
        .filter(LCMaster.status.in_(["OPEN", "SHIPPED"]))
        .all()
    )
    for lc in open_lcs_list:
        bank = (lc.bank_name or "Unknown").strip()
        amt = sum(float(p.lc_amount or 0) for p in (lc.products or []))
        bank_map[bank]["lc_amount"] += amt
        bank_map[bank]["lc_count"] += 1
    bank_exposure = sorted(
        [{"bank": b, **v} for b, v in bank_map.items()],
        key=lambda x: -x["lc_amount"],
    )[:8]

    # Average LC opening days (contract sent to bank → LC received)
    opening_days: list[int] = []
    for c in (
        db.query(Contract)
        .options(joinedload(Contract.lc))
        .filter(Contract.sent_to_bank_at.isnot(None))
        .all()
    ):
        if c.lc and c.lc.import_date and c.sent_to_bank_at:
            lc_dt = c.lc.import_date.date() if hasattr(c.lc.import_date, "date") else c.lc.import_date
            sent_dt = c.sent_to_bank_at.date() if hasattr(c.sent_to_bank_at, "date") else c.sent_to_bank_at
            opening_days.append((lc_dt - sent_dt).days)
    avg_lc_opening_days = round(sum(opening_days) / len(opening_days), 1) if opening_days else None

    # Demurrage & detention summary + per-shipment clock state
    bls = (
        db.query(BillOfLading)
        .options(joinedload(BillOfLading.shipment))
        .all()
    )
    dem_accruing = dem_cleared = det_accruing = 0
    demurrage_paid = detention_paid = 0.0
    ship_clock: dict[int, dict[str, str | None]] = defaultdict(lambda: {"demurrage_state": None, "detention_state": None})
    for bl in bls:
        sid = bl.shipment_id
        if bl.bl_type == "CONTAINER":
            det = compute_container_detention(bl, dem_cfg)
            st = det.get("state")
            if st == "ACCRUING":
                det_accruing += 1
                if sid:
                    ship_clock[sid]["detention_state"] = "ACCRUING"
            detention_paid += float(det.get("total_amount") or 0)
        else:
            dem = compute_demurrage(bl, dem_cfg)
            st = dem.get("state")
            if st == "ACCRUING":
                dem_accruing += 1
                if sid:
                    ship_clock[sid]["demurrage_state"] = "ACCRUING"
            elif st == "CLEARED":
                dem_cleared += 1
                if sid and ship_clock[sid]["demurrage_state"] != "ACCRUING":
                    ship_clock[sid]["demurrage_state"] = "CLEARED"
            demurrage_paid += float(dem.get("total_amount") or 0)

    # Quota by company (aggregate SRO approvals)
    usage = compute_usage(db)
    quota_by_company: dict[str, dict] = defaultdict(
        lambda: {"approved_mt": 0.0, "used_mt": 0.0, "available_mt": 0.0}
    )
    for approval in db.query(EdbApproval).all():
        row = approval_to_dict(approval, db, usage)
        code = row.get("company_code") or resolver.resolve(row.get("company_name")).get("short_code")
        company = code or row.get("company_name") or "Unknown"
        approved = float(row.get("approved_qty_mt") or 0)
        used = float(row.get("consumed_qty_mt") or 0)
        quota_by_company[company]["approved_mt"] += approved
        quota_by_company[company]["used_mt"] += used
        quota_by_company[company]["available_mt"] += max(0.0, approved - used)
    quota_allocation = sorted(
        [
            {
                "company": k,
                "approved_mt": round(v["approved_mt"], 2),
                "used_mt": round(v["used_mt"], 2),
                "available_mt": round(v["available_mt"], 2),
            }
            for k, v in quota_by_company.items()
            if v["approved_mt"] > 0
        ],
        key=lambda x: -x["approved_mt"],
    )

    # Drill-down shipment rows — full list for interactive dashboard filtering
    drilldown_shipments = []
    for s in ships:
        lc = s.lc
        prod = lc.products[0] if lc and lc.products else None
        qty_mt = float(s.total_net_weight_mt or s.delivered_quantity_mt or 0)
        expected = float(s.expected_quantity_mt or 0)
        eta_flag = None
        if s.eta:
            days = (s.eta - today).days
            if days < 0:
                eta_flag = "overdue"
            elif days <= 7:
                eta_flag = "soon"
        eta_label, eta_color = _eta_status_label(s, today)
        gd = s.goods_declarations[0] if s.goods_declarations else None
        company_code = None
        if lc and lc.importer_name:
            company_code = resolver.resolve(lc.importer_name).get("short_code")
        clocks = ship_clock.get(s.shipment_id, {})
        drilldown_shipments.append({
            "shipment_id": s.shipment_id,
            "shipment_ref": s.shipment_ref or f"SH-{s.shipment_id:04d}",
            "lc_number": lc.lc_number if lc else None,
            "vessel_name": s.vessel_name,
            "eta": s.eta.isoformat() if s.eta else None,
            "eta_flag": eta_flag,
            "status": s.status,
            "status_bucket": _shipment_bucket(s.status),
            "eta_status": eta_label,
            "eta_status_color": eta_color,
            "country": (prod.origin or "").strip() if prod else None,
            "item_category": (
                (prod.item_code or prod.product_code or prod.product_name or "Other").strip()
                if prod else None
            ),
            "bank": (lc.bank_name or "").strip() if lc else None,
            "supplier": (lc.supplier_name or "").strip() if lc else None,
            "company_code": company_code,
            "importer": lc.importer_name if lc else None,
            "month": s.created_at.date().replace(day=1).isoformat() if s.created_at else None,
            "qty_mt": round(qty_mt, 3) if qty_mt else None,
            "qty_kgs": round(qty_mt * 1000, 0) if qty_mt else None,
            "gd_type": gd.gd_type if gd else None,
            "demurrage_state": clocks.get("demurrage_state"),
            "detention_state": clocks.get("detention_state"),
            "has_short_shipment": bool(expected > 0 and qty_mt < expected),
        })

    shipment_table = sorted(
        drilldown_shipments,
        key=lambda r: (r["eta"] is None, r["eta"] or "9999"),
    )[:30]

    country_imports = sorted(
        [
            {"country": k, "qty_kgs": round(v * 1000, 0), "qty_mt": round(v, 3)}
            for k, v in country_qty_mt.items()
        ],
        key=lambda x: -x["qty_kgs"],
    )

    monthly_series = sorted(
        [{"month": k, "shipments": v} for k, v in monthly_trend.items()],
        key=lambda x: x["month"],
    )[-12:]

    item_categories = sorted(
        [{"item": k, "qty_mt": round(v, 3)} for k, v in item_qty_mt.items()],
        key=lambda x: -x["qty_mt"],
    )[:10]

    supplier_performance = sorted(
        [
            {
                "supplier": k,
                "ship_qty_mt": round(v["qty_mt"], 3),
                "short_shipment_mt": round(v["short_mt"], 3),
                "item": ", ".join(sorted(v["items"]))[:80] or None,
            }
            for k, v in supplier_agg.items()
            if v["qty_mt"] > 0
        ],
        key=lambda x: -x["ship_qty_mt"],
    )[:8]

    return {
        "kpis": {
            "total_shipments": total_shipments,
            "total_containers": int(total_containers),
            "total_qty_kgs": round(total_qty_kgs, 0),
            "total_lcs": total_lcs,
            "open_lcs": open_lcs,
            "lc_amount_usd": round(lc_amount_usd, 2),
            "gd_count": gd_count,
            "duty_total_pkr": round(duty_total_pkr, 2),
            "countries_count": len(country_qty_mt),
            "avg_lc_opening_days": avg_lc_opening_days,
        },
        "eta_dashboard": eta_rows[:15],
        "bank_exposure": bank_exposure,
        "country_imports": country_imports,
        "monthly_trend": monthly_series,
        "shipment_status_pie": [
            {"status": k, "count": v} for k, v in shipment_buckets.items()
        ],
        "item_categories": item_categories,
        "demurrage_detention": {
            "demurrage_accruing": dem_accruing,
            "demurrage_cleared": dem_cleared,
            "detention_accruing": det_accruing,
            "demurrage_paid": round(demurrage_paid, 2),
            "detention_paid": round(detention_paid, 2),
        },
        "gd_breakdown": {
            "home_consumption": gd_type_counts.get("HOME_CONSUMPTION", 0),
            "into_bond": gd_type_counts.get("INTO_BOND", 0),
            "ex_bond": gd_type_counts.get("EX_BOND", 0),
            "total": gd_count,
        },
        "quota_allocation": quota_allocation,
        "supplier_performance": supplier_performance,
        "drilldown_shipments": drilldown_shipments,
        "shipment_table": shipment_table,
    }
