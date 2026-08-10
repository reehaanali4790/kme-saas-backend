"""
KPT-triggered document alerts.

1) ETA received  -> BL + Commercial Invoice + Packing List missing
2) On port       -> GD document missing
3) Departed +3d  -> pick inventory from port (per BL), until delivery_date set

Re-alert cadence: daily while condition persists; after user acknowledges, wait
ACK_COOLDOWN_HOURS before raising again if still unresolved.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from config.settings import settings
from models.database_models import BillOfLading, Shipment, SystemAlert
from infrastructure.alerts.recurring_alerts import (
    ACK_COOLDOWN_HOURS,
    can_raise_recurring as _can_raise_recurring,
    create_recurring_alert,
    family_key as _family_key,
    latest_family_alert as _latest_family_alert,
    next_dedup_key as _next_dedup_key,
    resolve_family as _resolve_family,
)

logger = logging.getLogger("uvicorn")

ALERT_ETA_DOCS = "KPT_ETA_DOCS_MISSING"
ALERT_ON_PORT_GD = "KPT_ON_PORT_GD_MISSING"
ALERT_PORT_PICKUP = "KPT_PORT_PICKUP_DUE"

DEPARTURE_PICKUP_DAYS = 3


def _ref(s: Shipment) -> str:
    return s.shipment_ref or f"SH-{s.shipment_id:04d}"


def _has_uploaded(records, path_attr: str = "document_path") -> bool:
    return any(getattr(r, path_attr, None) for r in (records or []))


def missing_eta_documents(s: Shipment) -> List[str]:
    missing = []
    if not _has_uploaded(s.bill_of_ladings):
        missing.append("BL")
    if not _has_uploaded(s.commercial_invoices):
        missing.append("Commercial Invoice")
    if not _has_uploaded(s.packing_lists):
        missing.append("Packing List")
    return missing


def has_gd_document(s: Shipment) -> bool:
    for gd in s.goods_declarations or []:
        if gd.document_path or gd.gd_view_uploaded:
            return True
    return False


def is_delivery_complete(s: Shipment) -> bool:
    return bool(s.delivery_date) or (s.status or "").upper() == "DELIVERED"


_create_alert = create_recurring_alert


def _load_shipments(db: Session, shipment_ids: Optional[Sequence[int]] = None) -> List[Shipment]:
    q = (
        db.query(Shipment)
        .options(
            joinedload(Shipment.lc),
            joinedload(Shipment.bill_of_ladings),
            joinedload(Shipment.commercial_invoices),
            joinedload(Shipment.packing_lists),
            joinedload(Shipment.goods_declarations),
        )
        .filter(Shipment.is_deleted.is_(False))
    )
    if shipment_ids:
        q = q.filter(Shipment.shipment_id.in_(list(shipment_ids)))
    return q.all()


def scan_eta_document_alerts(
    db: Session,
    shipment_ids: Optional[Sequence[int]] = None,
    *,
    cooldown_hours: Optional[int] = None,
) -> dict:
    cooldown = cooldown_hours if cooldown_hours is not None else settings.KPT_DOC_ALERT_ACK_HOURS
    created = 0
    resolved = 0
    for s in _load_shipments(db, shipment_ids):
        if not s.kpt_eta_at and not s.eta:
            continue
        family = _family_key(ALERT_ETA_DOCS, s.shipment_id)
        missing = missing_eta_documents(s)
        if not missing:
            resolved += _resolve_family(db, family)
            continue
        eta_txt = s.eta.isoformat() if s.eta else "—"
        if _create_alert(
            db,
            alert_type=ALERT_ETA_DOCS,
            family_prefix=f"{family}:{'/'.join(missing)}",
            severity="HIGH",
            title=f"Missing {', '.join(missing)} — {_ref(s)}",
            message=(
                f"KPT ETA is set ({eta_txt}) for {_ref(s)}"
                f"{' (LC ' + s.lc.lc_number + ')' if s.lc else ''}, but "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} still not uploaded. "
                f"Upload the shipping documents to proceed with clearance."
            ),
            entity_type="SHIPMENT",
            entity_id=s.shipment_id,
            lc_id=s.lc_id,
            shipment_id=s.shipment_id,
            cooldown_hours=cooldown,
        ):
            created += 1
    return {"created": created, "resolved": resolved}


def scan_on_port_gd_alerts(
    db: Session,
    shipment_ids: Optional[Sequence[int]] = None,
    *,
    cooldown_hours: Optional[int] = None,
) -> dict:
    cooldown = cooldown_hours if cooldown_hours is not None else settings.KPT_DOC_ALERT_ACK_HOURS
    created = 0
    resolved = 0
    for s in _load_shipments(db, shipment_ids):
        if not s.kpt_on_port_at and (s.vessel_location or "").strip().lower() != "on port":
            continue
        family = _family_key(ALERT_ON_PORT_GD, s.shipment_id)
        if has_gd_document(s):
            resolved += _resolve_family(db, family)
            continue
        if _create_alert(
            db,
            alert_type=ALERT_ON_PORT_GD,
            family_prefix=family,
            severity="HIGH",
            title=f"GD document missing — {_ref(s)} on port",
            message=(
                f"Vessel for {_ref(s)}"
                f"{' (LC ' + s.lc.lc_number + ')' if s.lc else ''} is on port at KPT, "
                f"but no Goods Declaration document has been uploaded. File and upload the GD "
                f"to avoid clearance delays."
            ),
            entity_type="SHIPMENT",
            entity_id=s.shipment_id,
            lc_id=s.lc_id,
            shipment_id=s.shipment_id,
            cooldown_hours=cooldown,
        ):
            created += 1
    return {"created": created, "resolved": resolved}


def scan_departure_pickup_alerts(
    db: Session,
    shipment_ids: Optional[Sequence[int]] = None,
    *,
    cooldown_hours: Optional[int] = None,
    pickup_days: Optional[int] = None,
) -> dict:
    cooldown = cooldown_hours if cooldown_hours is not None else settings.KPT_DOC_ALERT_ACK_HOURS
    grace = pickup_days if pickup_days is not None else settings.KPT_DEPARTURE_PICKUP_DAYS
    created = 0
    resolved = 0
    now = datetime.utcnow()
    for s in _load_shipments(db, shipment_ids):
        if not s.kpt_departed_at:
            continue
        if is_delivery_complete(s):
            for bl in s.bill_of_ladings or []:
                resolved += _resolve_family(db, _family_key(ALERT_PORT_PICKUP, s.shipment_id, str(bl.bl_id)))
            resolved += _resolve_family(db, _family_key(ALERT_PORT_PICKUP, s.shipment_id, "shipment"))
            continue
        days_since = (now - s.kpt_departed_at).days
        if days_since < grace:
            continue
        targets: Iterable[tuple[str, str, int, str]] = []
        if s.bill_of_ladings:
            for bl in s.bill_of_ladings:
                bl_ref = bl.bl_number or f"BL-{bl.bl_id}"
                targets = list(targets) + [(str(bl.bl_id), "BL", bl.bl_id, bl_ref)]
        else:
            targets = [("shipment", "SHIPMENT", s.shipment_id, _ref(s))]
        for suffix, entity_type, entity_id, label in targets:
            family = _family_key(ALERT_PORT_PICKUP, s.shipment_id, suffix)
            if _create_alert(
                db,
                alert_type=ALERT_PORT_PICKUP,
                family_prefix=family,
                severity="HIGH",
                title=f"Pick inventory from port — {label}",
                message=(
                    f"{label} for {_ref(s)}"
                    f"{' (LC ' + s.lc.lc_number + ')' if s.lc else ''} departed KPT "
                    f"{days_since} day(s) ago. Arrange port pickup and complete delivery "
                    f"before demurrage accrues."
                ),
                entity_type=entity_type,
                entity_id=entity_id,
                lc_id=s.lc_id,
                shipment_id=s.shipment_id,
                cooldown_hours=cooldown,
            ):
                created += 1
    return {"created": created, "resolved": resolved}


def run_kpt_document_alert_scan(
    db: Session,
    shipment_ids: Optional[Sequence[int]] = None,
) -> dict:
    eta = scan_eta_document_alerts(db, shipment_ids)
    gd = scan_on_port_gd_alerts(db, shipment_ids)
    pickup = scan_departure_pickup_alerts(db, shipment_ids)
    summary = {
        "eta_docs": eta,
        "on_port_gd": gd,
        "port_pickup": pickup,
        "total_created": eta["created"] + gd["created"] + pickup["created"],
        "total_resolved": eta["resolved"] + gd["resolved"] + pickup["resolved"],
    }
    logger.info("[KPT doc alerts] %s", summary)
    return summary


def stamp_kpt_eta(db: Session, shipments: Sequence[Shipment]) -> None:
    now = datetime.utcnow()
    for s in shipments:
        if not s.kpt_eta_at:
            s.kpt_eta_at = now


def stamp_kpt_on_port(db: Session, shipments: Sequence[Shipment]) -> None:
    now = datetime.utcnow()
    for s in shipments:
        if not s.kpt_on_port_at:
            s.kpt_on_port_at = now


def stamp_kpt_departed(db: Session, shipments: Sequence[Shipment]) -> None:
    now = datetime.utcnow()
    for s in shipments:
        if not s.kpt_departed_at:
            s.kpt_departed_at = now
