"""
Business logic for the operations dashboard, extracted from
modules/reports/dashboard_router.py as part of the Phase 4 module rollout.
"""
from datetime import date, timedelta
from sqlalchemy import func, event
from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    LCMaster, Shipment, GoodsDeclaration, SystemAlert, Contract,
)


def summary(db: Session) -> dict:
    import orjson
    from core.redis import redis_cache

    cache_key = "lme:dashboard:summary"
    cached_data = redis_cache.get(cache_key)
    if cached_data:
        try:
            return orjson.loads(cached_data)
        except Exception:
            pass

    today = date.today()
    soon = today + timedelta(days=7)
    month_start = today.replace(day=1)

    # ---- KPIs ----
    total_lcs = db.query(func.count(LCMaster.lc_id)).scalar() or 0
    open_lcs = db.query(func.count(LCMaster.lc_id)).filter(LCMaster.status == "OPEN").scalar() or 0
    expiring_7d = db.query(func.count(LCMaster.lc_id)).filter(
        LCMaster.status.in_(["OPEN", "SHIPPED"]),
        LCMaster.expiry_date.isnot(None),
        LCMaster.expiry_date >= today, LCMaster.expiry_date <= soon,
    ).scalar() or 0

    total_shipments = db.query(func.count(Shipment.shipment_id)).filter(
        Shipment.is_deleted.is_(False)).scalar() or 0
    active_shipments = db.query(func.count(Shipment.shipment_id)).filter(
        Shipment.is_deleted.is_(False),
        Shipment.status.notin_(["PAYMENT_MADE", "ACCEPTED", "DELIVERED"])).scalar() or 0
    discrepant = db.query(func.count(Shipment.shipment_id)).filter(
        Shipment.is_deleted.is_(False),
        Shipment.validation_status == "DISCREPANT").scalar() or 0

    active_alerts = db.query(func.count(SystemAlert.alert_id)).filter(SystemAlert.status == "ACTIVE").scalar() or 0
    high_alerts = db.query(func.count(SystemAlert.alert_id)).filter(
        SystemAlert.status == "ACTIVE", SystemAlert.severity == "HIGH").scalar() or 0

    duty_month = db.query(func.coalesce(func.sum(GoodsDeclaration.total_duties_pkr), 0)).filter(
        GoodsDeclaration.filing_date >= month_start).scalar() or 0
    duty_total = db.query(func.coalesce(func.sum(GoodsDeclaration.total_duties_pkr), 0)).scalar() or 0
    shipped_mt = db.query(func.coalesce(func.sum(Shipment.delivered_quantity_mt), 0)).filter(
        Shipment.is_deleted.is_(False)).scalar() or 0

    # ---- LC status breakdown ----
    lc_status = {s: c for s, c in db.query(LCMaster.status, func.count(LCMaster.lc_id))
                     .group_by(LCMaster.status).all()}

    # ---- Shipment status breakdown ----
    ship_status = {s: c for s, c in db.query(Shipment.status, func.count(Shipment.shipment_id))
                       .filter(Shipment.is_deleted.is_(False))
                       .group_by(Shipment.status).all()}

    # ---- Alerts by severity ----
    alert_sev = {s: c for s, c in db.query(SystemAlert.severity, func.count(SystemAlert.alert_id))
                     .filter(SystemAlert.status == "ACTIVE").group_by(SystemAlert.severity).all()}

    # ---- Top suppliers by LC count ----
    top_suppliers = [
        {"supplier": s or "(Unknown)", "count": c}
        for s, c in db.query(LCMaster.supplier_name, func.count(LCMaster.lc_id))
        .filter(LCMaster.supplier_name.isnot(None))
        .group_by(LCMaster.supplier_name)
        .order_by(func.count(LCMaster.lc_id).desc()).limit(6).all()
    ]

    # ---- Upcoming deadlines (next 14 days) ----
    horizon = today + timedelta(days=14)
    deadlines = []
    for lc in db.query(LCMaster).options(joinedload(LCMaster.shipments)).filter(
        LCMaster.status.in_(["OPEN", "SHIPPED"]),
        LCMaster.expiry_date.isnot(None),
        LCMaster.expiry_date >= today, LCMaster.expiry_date <= horizon,
    ).order_by(LCMaster.expiry_date.asc()).limit(8).all():
        # Soonest-ETA vessel among this LC's shipments (if any) — surfaced alongside the
        # expiry so the deadline card also answers "which vessel is this LC riding on".
        vessel_ships = [s for s in (lc.shipments or []) if not s.is_deleted and s.vessel_name]
        vessel_ships.sort(key=lambda s: (s.eta is None, s.eta))
        vessel = vessel_ships[0] if vessel_ships else None
        deadlines.append({
            "lc_id": lc.lc_id, "lc_number": lc.lc_number, "type": "LC Expiry",
            "date": lc.expiry_date.isoformat(),
            "days": (lc.expiry_date - today).days,
            "vessel_name": (vessel.vessel_name if vessel else lc.vessel_name) or None,
            "vessel_date": (vessel.eta.isoformat() if vessel and vessel.eta
                             else (lc.eta.isoformat() if lc.eta else None)),
        })

    # ---- Contract pipeline: how many purchase contracts have an LC opened against them
    # yet, vs how many are still awaiting the bank to issue one. Mirrors Contract's own
    # DRAFT -> FINAL -> ACTUAL lifecycle (ACTUAL = LC linked); cancelled contracts are
    # excluded since they were never going anywhere.
    total_contracts = db.query(func.count(Contract.contract_id)).filter(
        Contract.status != "CANCELLED").scalar() or 0
    contracts_with_lc = db.query(func.count(Contract.contract_id)).filter(
        Contract.status != "CANCELLED",
        Contract.contract_id.in_(db.query(LCMaster.contract_id).filter(LCMaster.contract_id.isnot(None))),
    ).scalar() or 0
    contract_pipeline = {
        "total_contracts": total_contracts,
        "contracts_with_lc": contracts_with_lc,
        "contracts_awaiting_lc": total_contracts - contracts_with_lc,
    }

    # ---- Top active high/medium alerts ----
    top_alerts = []
    for a in db.query(SystemAlert).filter(SystemAlert.status == "ACTIVE").all():
        top_alerts.append(a)
    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    top_alerts.sort(key=lambda a: (sev_order.get(a.severity, 9),
                                   -(a.created_at.timestamp() if a.created_at else 0)))
    top_alerts = [{
        "alert_id": a.alert_id, "alert_type": a.alert_type, "severity": a.severity,
        "title": a.title, "lc_id": a.lc_id, "shipment_id": a.shipment_id,
    } for a in top_alerts[:6]]

    # ---- Recent shipments ----
    recent = []
    for s in db.query(Shipment).filter(Shipment.is_deleted.is_(False)) \
            .order_by(Shipment.created_at.desc()).limit(6).all():
        recent.append({
            "shipment_id": s.shipment_id,
            "shipment_ref": s.shipment_ref or f"SH-{s.shipment_id:04d}",
            "lc_number": s.lc.lc_number if s.lc else None,
            "vessel_name": s.vessel_name, "status": s.status,
            "validation_status": s.validation_status,
        })

    result = {
        "kpis": {
            "total_lcs": total_lcs, "open_lcs": open_lcs, "expiring_7d": expiring_7d,
            "total_shipments": total_shipments, "active_shipments": active_shipments,
            "discrepancies": discrepant, "active_alerts": active_alerts, "high_alerts": high_alerts,
            "duty_month_pkr": float(duty_month), "duty_total_pkr": float(duty_total),
            "shipped_mt": float(shipped_mt),
        },
        "lc_status": lc_status,
        "shipment_status": ship_status,
        "alert_severity": alert_sev,
        "top_suppliers": top_suppliers,
        "deadlines": deadlines,
        "top_alerts": top_alerts,
        "recent_shipments": recent,
        "contract_pipeline": contract_pipeline,
    }

    # Cache result for 7 days
    try:
        redis_cache.set(cache_key, orjson.dumps(result).decode("utf-8"), ex=604800)
    except Exception:
        pass

    return result


def arrivals(db: Session) -> dict:
    """Upcoming vessel arrivals — the primary operations panel.
    Shipments with an ETA, not yet completed, with the details the import team needs."""
    import orjson
    from core.redis import redis_cache

    cache_key = "lme:dashboard:arrivals"
    cached_data = redis_cache.get(cache_key)
    if cached_data:
        try:
            return orjson.loads(cached_data)
        except Exception:
            pass

    today = date.today()
    grace = today - timedelta(days=7)   # keep just-arrived visible for a week

    ships = db.query(Shipment).options(
        joinedload(Shipment.lc),
        joinedload(Shipment.goods_declarations),
    ).filter(
        Shipment.is_deleted.is_(False),
        Shipment.eta.isnot(None),
        Shipment.eta >= grace,
        # Delivered / paid / accepted shipments are closed — keep them out of arrivals.
        Shipment.status.notin_(["PAYMENT_MADE", "ACCEPTED", "DELIVERED"]),
    ).all()

    # Sort ships:
    # 1. Upcoming vessels (ETA >= today) FIRST, ordered by soonest arrival first (ascending ETA).
    # 2. Recently arrived vessels (ETA < today) SECOND, ordered by most recent arrival first (descending ETA).
    def arrival_sort_key(s: Shipment):
        is_past = 1 if s.eta < today else 0
        if is_past:
            return (1, -s.eta.toordinal())
        else:
            return (0, s.eta.toordinal())

    ships.sort(key=arrival_sort_key)

    def n(v):
        return float(v) if v is not None else None

    items = []
    for s in ships:
        inv = s.commercial_invoices[0] if s.commercial_invoices else None
        bl = s.bill_of_ladings[0] if s.bill_of_ladings else None
        lc = s.lc
        items.append({
            "shipment_id": s.shipment_id,
            "lc_number": lc.lc_number if lc else None,
            "lot_number": s.lot_number,
            "vessel_name": s.vessel_name,
            "voyage_number": s.voyage_number,
            "eta": s.eta.isoformat(),
            "eta_source": s.eta_source,
            "days_to_arrival": (s.eta - today).days,
            "port_of_discharge": s.port_of_discharge,
            "bl_number": bl.bl_number if bl else None,
            "total_coils": s.total_coils if s.total_coils is not None else (inv.total_coils if inv else None),
            "quantity_mt": n(s.total_net_weight_mt) or (n(inv.total_net_weight_mt) if inv else None),
            "invoice_amount_usd": n(inv.total_amount_usd) if inv else None,
            "invoice_qty_mt": n(inv.total_net_weight_mt) if inv else None,
            "importer_name": (lc.importer_name if lc else None),
            "indentor": (lc.indentor if lc else None),
            "hoa": (lc.hoa if lc else None),
            # GD file uploaded? true if any GD on the shipment has a stored document.
            "gd_uploaded": any(bool(g.document_path) for g in s.goods_declarations),
            "delivery_date": s.delivery_date.isoformat() if s.delivery_date else None,
            "status": s.status,
        })
    result = {"count": len(items), "items": items}

    # Cache result for 5 minutes (300s)
    try:
        redis_cache.set(cache_key, orjson.dumps(result).decode("utf-8"), ex=300)
    except Exception:
        pass

    return result


# Register SQLAlchemy event listeners for automatic dashboard cache invalidation
def invalidate_dashboard_cache():
    print("\n--- CACHE INVALIDATE TRIGGERED ---")
    from core.redis import redis_cache
    try:
        redis_cache.delete("lme:dashboard:summary")
        redis_cache.delete("lme:dashboard:arrivals")
        redis_cache.delete("lme:dashboard:v2:summary")
    except Exception:
        pass


@event.listens_for(Session, "after_flush")
def after_flush_listener(session, flush_context):
    # Check if any new, dirty, or deleted object is one of our dashboard target models
    for obj in session.new | session.dirty | session.deleted:
        if isinstance(obj, (LCMaster, Shipment, GoodsDeclaration, SystemAlert, Contract)):
            invalidate_dashboard_cache()
            break
