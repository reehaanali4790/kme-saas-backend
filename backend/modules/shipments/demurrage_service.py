"""
Demurrage clock — single source of truth.

Given a BillOfLading and the global DemurrageConfig, compute the current demurrage
state, the last free day, days remaining, chargeable days and the accrued charge.
Used by both the BL endpoints (for display) and the alert engine (for alerting),
so the two never disagree.

States:
  OK        — start date set, still inside free time (more than warn_days left)
  AT_RISK   — inside free time but last free day is within warn_days
  ACCRUING  — past the last free day, charges accruing (not yet cleared/delivered)
  CLEARED   — cargo picked up OR related shipment is DELIVERED; clock frozen
  UNKNOWN   — not enough data (no start date or no free days)

Accrued dollar amount is NEVER formula-estimated — only demurrage_total_amount
entered by the user (typically on the Demurrage Report once the shipment is delivered).
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any


def _to_decimal(v):
    return Decimal(str(v)) if v is not None else None


def _shipment_delivered(shipment) -> bool:
    if not shipment:
        return False
    return bool(getattr(shipment, "delivery_date", None)) or (getattr(shipment, "status", None) or "").upper() == "DELIVERED"


def _effective_cleared_date(bl, shipment, today: date | None = None) -> date | None:
    """Clock stop: explicit BL cleared date, or shipment delivery when delivered."""
    if bl.demurrage_cleared_date:
        return bl.demurrage_cleared_date
    if _shipment_delivered(shipment):
        return getattr(shipment, "delivery_date", None) or (today or date.today())
    return None


def close_bl_demurrage_on_delivery(shipment) -> None:
    """When a shipment is marked delivered, freeze demurrage on all coil BLs under it."""
    if not _shipment_delivered(shipment):
        return
    stop = getattr(shipment, "delivery_date", None) or date.today()
    for bl in getattr(shipment, "bill_of_ladings", None) or []:
        if getattr(bl, "bl_type", None) == "CONTAINER":
            continue
        if not bl.demurrage_start_date:
            bl.demurrage_start_date = (
                getattr(shipment, "on_port_date", None) or bl.bl_date or stop
            )
        if not bl.demurrage_cleared_date:
            bl.demurrage_cleared_date = stop


def compute_demurrage(bl, config, today: date | None = None) -> dict:
    """Return the demurrage status block for a BL.

    bl     — BillOfLading (uses demurrage_start_date, free_days,
             demurrage_total_amount, demurrage_currency, demurrage_cleared_date)
    config — DemurrageConfig (used only for warn_days)
    today  — override for testing; defaults to date.today()
    """
    today = today or date.today()
    warn_days = (config.warn_days if config and config.warn_days is not None else 3)
    currency = bl.demurrage_currency or (config.currency if config else "USD")
    shipment = getattr(bl, "shipment", None)

    start = bl.demurrage_start_date or bl.bl_date or (shipment.on_port_date if shipment else None) or (shipment.eta if shipment else None)
    free_days = bl.free_days if bl.free_days is not None else (config.free_days if config and config.free_days is not None else 14)
    total = _to_decimal(getattr(bl, "demurrage_total_amount", None))
    cleared = _effective_cleared_date(bl, shipment, today)
    delivered = _shipment_delivered(shipment)
    total_float = float(total) if total is not None else None
    recorded_charge = total_float if total_float is not None else 0.0

    base = {
        "state": "UNKNOWN",
        "start_date": start.isoformat() if start else None,
        "free_days": free_days,
        "total_amount": total_float,
        "currency": currency,
        "cleared_date": cleared.isoformat() if cleared else None,
        "last_free_day": None,
        "days_remaining": None,
        "chargeable_days": 0,
        "accrued_charge": recorded_charge,
        "shipment_delivered": delivered,
        "can_enter_amount": False,
    }

    if not start or free_days is None:
        if delivered:
            stop = cleared or today
            base.update({
                "state": "CLEARED",
                "cleared_date": stop.isoformat() if stop else None,
                "can_enter_amount": True,
                "shipment_delivered": True,
            })
        return base

    last_free_day = start + timedelta(days=int(free_days))
    base["last_free_day"] = last_free_day.isoformat()

    if cleared or delivered:
        stop = cleared or today
        chargeable = max(0, (stop - last_free_day).days)
        base.update({
            "state": "CLEARED",
            "days_remaining": (last_free_day - stop).days,
            "chargeable_days": chargeable,
            "accrued_charge": recorded_charge,
            "cleared_date": stop.isoformat() if stop else None,
            "can_enter_amount": True,
        })
        return base

    days_remaining = (last_free_day - today).days
    base["days_remaining"] = days_remaining

    if today > last_free_day:
        chargeable = (today - last_free_day).days
        base.update({
            "state": "ACCRUING",
            "chargeable_days": chargeable,
            "accrued_charge": recorded_charge,
        })
    elif days_remaining <= warn_days:
        base["state"] = "AT_RISK"
    else:
        base["state"] = "OK"

    return base


# ---------------------------------------------------------------------------
# Config CRUD + at-risk BL listing
#
# Extracted from modules/shipments/demurrage_router.py as part of the Phase 4 module
# rollout.
# ---------------------------------------------------------------------------
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, joinedload

from infrastructure.normalization.normalization_service import company_resolver, matches_company_code

if TYPE_CHECKING:
    from modules.shipments.demurrage_schemas import DemurrageConfigUpdate

# Ranking for the at-risk list — most urgent first
STATE_RANK = {"ACCRUING": 0, "AT_RISK": 1, "OK": 2, "CLEARED": 3, "UNKNOWN": 4}


def config_to_dict(c) -> dict:
    return {
        "config_id": c.config_id,
        "free_days": c.free_days,
        "per_day_charge": float(c.per_day_charge) if c.per_day_charge is not None else None,
        "currency": c.currency,
        "warn_days": c.warn_days,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def get_or_create_config(db: Session):
    from models.database_models import DemurrageConfig
    cfg = db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()
    if not cfg:
        cfg = DemurrageConfig(free_days=7, per_day_charge=0, currency="USD", warn_days=3)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def update_config(data: "DemurrageConfigUpdate", db: Session):
    cfg = get_or_create_config(db)
    if data.free_days is not None:
        cfg.free_days = data.free_days
    if data.per_day_charge is not None:
        cfg.per_day_charge = data.per_day_charge
    if data.currency:
        cfg.currency = data.currency.strip().upper()[:10]
    if data.warn_days is not None:
        cfg.warn_days = data.warn_days
    db.commit()
    db.refresh(cfg)
    return cfg


def demurrage_report(
    db: Session,
    state: str | None = None,
    date_from=None,
    date_to=None,
    vessel: str | None = None,
    port: str | None = None,
    importer: str | None = None,
    q: str | None = None,
    open_only: bool = False,
) -> dict:
    """Filterable, one-row-per-BL demurrage report (coil cargo only — container BLs
    have their own Container Detention report). Reuses compute_demurrage() so the
    figures always agree with the BL list/edit views and the at-risk endpoint."""
    from models.database_models import BillOfLading, Shipment

    cfg = get_or_create_config(db)
    resolver = company_resolver(db)

    vessel_list = []
    if vessel:
        if isinstance(vessel, str):
            vessel_list = [v.strip() for v in vessel.split(",") if v.strip()]
        else:
            vessel_list = [v.strip() for v in vessel if v.strip()]

    port_list = []
    if port:
        if isinstance(port, str):
            port_list = [p.strip() for p in port.split(",") if p.strip()]
        else:
            port_list = [p.strip() for p in port if p.strip()]

    bls = (
        db.query(BillOfLading)
        .options(
            joinedload(BillOfLading.lc),
            joinedload(BillOfLading.shipment).joinedload(Shipment.lc),
        )
        .order_by(BillOfLading.bl_id.desc())
        .all()
    )

    vessels, ports, importers = set(), set(), set()
    rows: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        "bl_count": 0,
        "accruing_count": 0,
        "at_risk_count": 0,
        "ok_count": 0,
        "cleared_count": 0,
        "unknown_count": 0,
        "open_accrued": defaultdict(float),
        "cleared_charges": defaultdict(float),
        "total_recorded": defaultdict(float),
    }

    for bl in bls:
        if bl.bl_type == "CONTAINER":
            continue

        dem = compute_demurrage(bl, cfg)
        lc = bl.lc
        imp = lc.importer_name if lc else None
        imp_res = resolver.resolve(imp)
        from infrastructure.normalization.normalization_service import norm_vessel
        veh = norm_vessel(bl.vessel_name) or bl.vessel_name
        pod = bl.port_of_discharge

        if veh:
            vessels.add(veh)
        if pod:
            ports.add(pod)
        if imp:
            code = imp_res.get("short_code")
            if code:
                importers.add(code)

        st = dem["state"]
        row_total = dem.get("total_amount")
        start_d = bl.demurrage_start_date or bl.bl_date or (bl.shipment.on_port_date if getattr(bl, "shipment", None) else None) or (bl.shipment.eta if getattr(bl, "shipment", None) else None)

        if open_only and st == "CLEARED" and row_total is not None:
            continue
        if state and st != state:
            continue
        if vessel_list and veh not in vessel_list:
            continue
        if port_list and pod not in port_list:
            continue
        if importer and not matches_company_code(resolver, imp, importer):
            continue
        if date_from and (not start_d or start_d < date_from):
            continue
        if date_to and (not start_d or start_d > date_to):
            continue
        if q:
            needle = q.upper()
            ship = bl.shipment
            ship_ref = (ship.shipment_ref or f"SH-{ship.shipment_id:04d}") if ship else None
            ship_lc = ship.lc.lc_number if ship and getattr(ship, "lc", None) else None
            if not any(needle in (h or "").upper() for h in
                       (bl.bl_number, lc.lc_number if lc else None, ship_lc, ship_ref, veh, pod, imp)):
                continue

        cur = dem.get("currency") or "USD"
        charge = float(dem.get("total_amount") or 0)

        totals["bl_count"] += 1
        if charge:
            totals["total_recorded"][cur] += charge
        if st == "ACCRUING":
            totals["accruing_count"] += 1
            if charge:
                totals["open_accrued"][cur] += charge
        elif st == "AT_RISK":
            totals["at_risk_count"] += 1
        elif st == "OK":
            totals["ok_count"] += 1
        elif st == "CLEARED":
            totals["cleared_count"] += 1
            if charge:
                totals["cleared_charges"][cur] += charge
        else:
            totals["unknown_count"] += 1

        rows.append({
            "bl_id": bl.bl_id,
            "bl_number": bl.bl_number,
            "lc_id": bl.lc_id,
            "lc_number": lc.lc_number if lc else None,
            "shipment_id": bl.shipment_id,
            "shipment_status": bl.shipment.status if bl.shipment else None,
            "importer_name": imp,
            "company_code": imp_res.get("short_code"),
            "company_matched": imp_res.get("matched"),
            "vessel_name": veh,
            "port_of_discharge": pod,
            "gross_weight_mt": float(bl.gross_weight_mt) if bl.gross_weight_mt is not None else None,
            "bl_date": bl.bl_date.isoformat() if bl.bl_date else None,
            "demurrage": dem,
        })

    rows.sort(key=lambda r: (
        STATE_RANK.get(r["demurrage"]["state"], 9),
        r["demurrage"].get("days_remaining") if r["demurrage"].get("days_remaining") is not None else 999,
        -(r["demurrage"].get("chargeable_days") or 0),
        r["bl_number"] or "",
    ))

    totals["open_accrued"] = dict(totals["open_accrued"])
    totals["cleared_charges"] = dict(totals["cleared_charges"])
    totals["total_recorded"] = dict(totals["total_recorded"])

    options = {
        "vessels": sorted(vessels),
        "ports": sorted(ports),
        "importers": sorted(importers),
        "states": ["ACCRUING", "AT_RISK", "OK", "CLEARED", "UNKNOWN"],
    }

    return {"options": options, "config": config_to_dict(cfg), "rows": rows, "totals": totals}


def at_risk_bls(db: Session) -> dict:
    from models.database_models import BillOfLading

    cfg = get_or_create_config(db)
    bls = db.query(BillOfLading) \
            .options(joinedload(BillOfLading.lc)) \
            .filter(
                BillOfLading.demurrage_start_date.isnot(None),
                BillOfLading.demurrage_cleared_date.is_(None),
            ).all()

    items: list[dict[str, Any]] = []
    for bl in bls:
        dem = compute_demurrage(bl, cfg)
        if dem["state"] in ("ACCRUING", "AT_RISK"):
            items.append({
                "bl_id": bl.bl_id,
                "bl_number": bl.bl_number,
                "lc_number": bl.lc.lc_number if bl.lc else None,
                "vessel_name": bl.vessel_name,
                "port_of_discharge": bl.port_of_discharge,
                "demurrage": dem,
            })

    items.sort(key=lambda x: (
        STATE_RANK.get(x["demurrage"]["state"], 9),
        x["demurrage"]["days_remaining"] if x["demurrage"]["days_remaining"] is not None else 0,
    ))
    total_accrued = sum(x["demurrage"]["accrued_charge"] for x in items)
    return {"count": len(items), "total_accrued": total_accrued, "items": items}


def update_bl_demurrage_amount(
    db: Session,
    bl_id: int,
    amount: Decimal | None,
    currency: str | None,
) -> dict:
    """Persist user-entered demurrage charge for a delivered/cleared BL."""
    from models.database_models import BillOfLading

    bl = (
        db.query(BillOfLading)
        .options(joinedload(BillOfLading.shipment))
        .filter(BillOfLading.bl_id == bl_id)
        .first()
    )
    if not bl:
        raise LookupError(f"BL {bl_id} not found")
    if bl.bl_type == "CONTAINER":
        raise ValueError("Container BLs use container detention, not demurrage")

    cfg = get_or_create_config(db)
    dem = compute_demurrage(bl, cfg)
    if not dem.get("can_enter_amount"):
        raise ValueError(
            "Demurrage amount can only be entered after the related shipment is delivered "
            "or a cleared date is recorded on the BL"
        )

    bl.demurrage_total_amount = amount
    if currency:
        bl.demurrage_currency = currency.strip().upper()[:10]
    db.commit()
    db.refresh(bl)
    return {
        "success": True,
        "bl_id": bl.bl_id,
        "demurrage_total_amount": float(bl.demurrage_total_amount) if bl.demurrage_total_amount is not None else None,
        "demurrage_currency": bl.demurrage_currency,
        "demurrage": compute_demurrage(bl, cfg),
    }
