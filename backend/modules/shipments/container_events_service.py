"""Per-container milestones and last-free-date (manual now; API-shaped for later visibility)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from core.exceptions import NotFoundError, ValidationError
from models.database_models import ContainerEvent, Shipment
from modules.shipments.bl_service import get_demurrage_config
from modules.shipments.demurrage_service import compute_demurrage
from modules.shipments.services import get_shipment_or_404, ordered_docs
from modules.shipments.shipment_metrics import resolve_container_numbers

EVENT_TYPES = ("LOADED", "DISCHARGED", "AVAILABLE", "GATE_OUT", "EMPTY_RETURN")
_ISO_CONTAINER = re.compile(r"\b[A-Z]{4}\d{7}\b", re.I)
_LFD_EVENTS = {"DISCHARGED", "AVAILABLE"}


def parse_container_numbers(shipment: Shipment) -> list[str]:
    text, _src = resolve_container_numbers(shipment)
    found = _ISO_CONTAINER.findall(text or "")
    if found:
        seen, out = set(), []
        for c in found:
            u = c.upper()
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out
    return []


def default_last_free_date(shipment: Shipment, db: Session, event_date: Optional[date],
                           event_type: str) -> Optional[date]:
    if event_type not in _LFD_EVENTS:
        return None
    bls = ordered_docs(shipment.bill_of_ladings) if shipment.bill_of_ladings else []
    config = get_demurrage_config(db)
    if bls:
        dem = compute_demurrage(bls[0], config)
        lfd = dem.get("last_free_date")
        if lfd:
            return date.fromisoformat(str(lfd)[:10]) if not isinstance(lfd, date) else lfd
    free = config.free_days if config and config.free_days is not None else 7
    start = event_date or shipment.on_port_date or shipment.eta
    return (start + timedelta(days=free)) if start else None


def _to_date(v) -> Optional[date]:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def event_to_dict(ev: ContainerEvent, today: Optional[date] = None) -> dict:
    today = today or date.today()
    days = (ev.last_free_date - today).days if ev.last_free_date else None
    return {
        "event_id": ev.event_id,
        "shipment_id": ev.shipment_id,
        "bl_id": ev.bl_id,
        "container_number": ev.container_number,
        "event_type": ev.event_type,
        "event_date": ev.event_date.isoformat() if ev.event_date else None,
        "last_free_date": ev.last_free_date.isoformat() if ev.last_free_date else None,
        "days_to_lfd": days,
        "notes": ev.notes,
        "source": ev.source or "MANUAL",
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


def list_container_state(shipment_id: int, db: Session) -> dict:
    s = get_shipment_or_404(shipment_id, db)
    events = (db.query(ContainerEvent)
                .filter(ContainerEvent.shipment_id == shipment_id)
                .order_by(ContainerEvent.event_date.asc().nullslast(),
                          ContainerEvent.event_id.asc())
                .all())
    known = parse_container_numbers(s)
    for ev in events:
        if ev.container_number and ev.container_number not in known:
            known.append(ev.container_number)
    today = date.today()
    nearest = None
    for ev in events:
        if ev.last_free_date and (nearest is None or ev.last_free_date < nearest):
            nearest = ev.last_free_date
    return {
        "shipment_id": shipment_id,
        "known_containers": known,
        "nearest_last_free_date": nearest.isoformat() if nearest else None,
        "days_to_nearest_lfd": (nearest - today).days if nearest else None,
        "events": [event_to_dict(ev, today) for ev in events],
    }


def create_event(shipment_id: int, data: dict, db: Session, user_id: Optional[int]) -> dict:
    from sqlalchemy.orm import joinedload
    s = get_shipment_or_404(shipment_id, db, options=[joinedload(Shipment.bill_of_ladings)])
    cn = (data.get("container_number") or "").strip().upper()
    et = (data.get("event_type") or "").strip().upper()
    if not cn:
        raise ValidationError("container_number is required")
    if et not in EVENT_TYPES:
        raise ValidationError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
    event_date = _to_date(data.get("event_date"))
    lfd = _to_date(data.get("last_free_date"))
    if lfd is None:
        lfd = default_last_free_date(s, db, event_date, et)
    ev = ContainerEvent(
        shipment_id=shipment_id,
        bl_id=data.get("bl_id"),
        container_number=cn,
        event_type=et,
        event_date=event_date,
        last_free_date=lfd,
        notes=(data.get("notes") or None),
        source=(data.get("source") or "MANUAL"),
        created_by=user_id,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return event_to_dict(ev)


def update_event(shipment_id: int, event_id: int, data: dict, db: Session) -> dict:
    ev = (db.query(ContainerEvent)
            .filter(ContainerEvent.event_id == event_id,
                    ContainerEvent.shipment_id == shipment_id)
            .first())
    if not ev:
        raise NotFoundError("Container event not found")
    if "container_number" in data and data["container_number"]:
        ev.container_number = str(data["container_number"]).strip().upper()
    if "event_type" in data and data["event_type"]:
        et = str(data["event_type"]).strip().upper()
        if et not in EVENT_TYPES:
            raise ValidationError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
        ev.event_type = et
    if "event_date" in data:
        ev.event_date = _to_date(data["event_date"])
    if "last_free_date" in data:
        ev.last_free_date = _to_date(data["last_free_date"])
    if "notes" in data:
        ev.notes = data["notes"] or None
    if "bl_id" in data:
        ev.bl_id = data["bl_id"]
    db.commit()
    db.refresh(ev)
    return event_to_dict(ev)


def delete_event(shipment_id: int, event_id: int, db: Session) -> None:
    ev = (db.query(ContainerEvent)
            .filter(ContainerEvent.event_id == event_id,
                    ContainerEvent.shipment_id == shipment_id)
            .first())
    if not ev:
        raise NotFoundError("Container event not found")
    db.delete(ev)
    db.commit()
