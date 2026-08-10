"""
Recurring SystemAlert mechanics shared by the KPT document alerts and the into-bond
180-day alerts.

The cadence, in one place so every recurring rule behaves identically:
  * an alert belongs to a FAMILY (a dedup_key prefix) rather than a single row
  * while a family has an ACTIVE alert, nothing new is raised
  * once the user acknowledges, the family goes quiet for ACK_COOLDOWN_HOURS, then
    re-alerts daily for as long as the underlying condition persists
  * when the condition clears, the whole family is RESOLVED — including alerts the
    user already acknowledged
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.database_models import SystemAlert

ACK_COOLDOWN_HOURS = 24

# Alert lifecycle states that still "count" as an open alert for a family.
OPEN_STATUSES = ["ACTIVE", "ACKNOWLEDGED"]


def family_key(alert_type: str, entity_id: int, extra: str = "") -> str:
    base = f"{alert_type}:{entity_id}"
    return f"{base}:{extra}" if extra else base


def latest_family_alert(db: Session, family_prefix: str) -> Optional[SystemAlert]:
    return (
        db.query(SystemAlert)
        .filter(
            SystemAlert.dedup_key.like(f"{family_prefix}%"),
            SystemAlert.status.in_(OPEN_STATUSES),
        )
        .order_by(SystemAlert.created_at.desc())
        .first()
    )


def can_raise_recurring(db: Session, family_prefix: str, cooldown_hours: int) -> bool:
    latest = latest_family_alert(db, family_prefix)
    if not latest:
        return True
    if latest.status == "ACTIVE":
        return False
    if latest.acknowledged_at:
        return datetime.utcnow() - latest.acknowledged_at >= timedelta(hours=cooldown_hours)
    return True


def resolve_family(db: Session, family_prefix: str) -> int:
    """Close every open alert in the family. Used when the condition no longer holds."""
    rows = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.dedup_key.like(f"{family_prefix}%"),
            SystemAlert.status.in_(OPEN_STATUSES),
        )
        .all()
    )
    for a in rows:
        a.status = "RESOLVED"
    return len(rows)


def next_dedup_key(db: Session, family_prefix: str) -> str:
    today = date.today().isoformat()
    base = f"{family_prefix}:{today}"
    if not db.query(SystemAlert.alert_id).filter(SystemAlert.dedup_key == base).first():
        return base
    n = 2
    while db.query(SystemAlert.alert_id).filter(SystemAlert.dedup_key == f"{base}:{n}").first():
        n += 1
    return f"{base}:{n}"


def create_recurring_alert(
    db: Session,
    *,
    alert_type: str,
    family_prefix: str,
    severity: str,
    title: str,
    message: str,
    entity_type: str,
    entity_id: int,
    lc_id: Optional[int] = None,
    shipment_id: Optional[int] = None,
    cooldown_hours: int = ACK_COOLDOWN_HOURS,
) -> bool:
    """Raise one alert for the family if the cadence allows it. True when raised."""
    if not can_raise_recurring(db, family_prefix, cooldown_hours):
        return False
    db.add(
        SystemAlert(
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            lc_id=lc_id,
            shipment_id=shipment_id,
            dedup_key=next_dedup_key(db, family_prefix),
            status="ACTIVE",
        )
    )
    return True


def create_once(
    db: Session,
    *,
    alert_type: str,
    dedup_key: str,
    severity: str,
    title: str,
    message: str,
    entity_type: str,
    entity_id: int,
    lc_id: Optional[int] = None,
    shipment_id: Optional[int] = None,
) -> bool:
    """Raise a one-shot alert keyed exactly. Never repeats, whatever its status."""
    if db.query(SystemAlert.alert_id).filter(SystemAlert.dedup_key == dedup_key).first():
        return False
    db.add(
        SystemAlert(
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            lc_id=lc_id,
            shipment_id=shipment_id,
            dedup_key=dedup_key,
            status="ACTIVE",
        )
    )
    return True
