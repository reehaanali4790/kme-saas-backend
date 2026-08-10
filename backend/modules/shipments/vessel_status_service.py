"""
Vessel port status / location resolution for reports and bulk updates.

Priority for the live status label:
  1. shipments.vessel_location (KPT crawler or manual — never stale static port fields)
  2. Infer from milestone timestamps when location text is empty
  3. Fall back to port_of_discharge / port_of_loading / LC origin

Standard statuses (manual menu + KPT normalisation):
  Expected Arrival, On port, Berth, Departed, Left Port

Ported from rehan/newchanges (backend/services/vessel_status_service.py).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models.database_models import Shipment, LCMaster, BillOfLading

# Canonical labels for the vessel-report manual dropdown.
STANDARD_STATUSES = (
    "Expected Arrival",
    "On port",
    "Berth",
    "Departed",
    "Left Port",
)

_STATUS_RANK = {
    "expected arrival": 1,
    "on port": 2,
    "berth": 3,
    "departed": 4,
    "left port": 5,
}


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return None


def _clean_location(raw: Optional[str]) -> Optional[str]:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s.upper().startswith("KPT:"):
        s = s[4:].strip()
    return s or None


def _status_key(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    low = label.lower()
    if low.startswith("berth"):
        return "berth"
    for key in _STATUS_RANK:
        if low == key or low.startswith(key):
            return key
    return low


def normalize_port_status(raw: Optional[str], berth: Optional[str] = None) -> str:
    """Map free-text / KPT values to a display status label."""
    loc = _clean_location(raw)
    if not loc:
        return "Expected Arrival"
    key = _status_key(loc)
    if key == "berth":
        b = (berth or "").strip()
        if not b and loc.lower().startswith("berth"):
            rest = loc[5:].strip(" -·")
            b = rest
        return f"Berth {b}".strip() if b else "Berth"
    if key in _STATUS_RANK:
        return {
            "expected arrival": "Expected Arrival",
            "on port": "On port",
            "berth": "Berth",
            "departed": "Departed",
            "left port": "Left Port",
        }[key]
    return loc


def _origin_fallback(shipment: Shipment, lc: Optional[LCMaster]) -> Optional[str]:
    if shipment.port_of_discharge:
        return shipment.port_of_discharge.strip()
    if shipment.port_of_loading:
        return shipment.port_of_loading.strip()
    if lc and lc.products:
        origin = (lc.products[0].origin or "").strip()
        if origin:
            return origin
    return None


def resolve_vessel_status(shipment: Shipment) -> dict:
    """Latest port status + milestone dates for one shipment."""
    lc = shipment.lc
    berth = (shipment.kpt_berth or "").strip() or None
    raw_loc = _clean_location(shipment.vessel_location)

    if raw_loc:
        status = normalize_port_status(raw_loc, berth)
    elif shipment.departure_date or shipment.kpt_departed_at:
        status = "Departed"
    elif shipment.on_port_date or shipment.kpt_on_port_at:
        status = f"Berth {berth}".strip() if berth else "On port"
    elif shipment.eta:
        status = "Expected Arrival"
    else:
        status = None

    on_port = _iso(shipment.on_port_date)
    departed = _iso(shipment.departure_date)
    eta = _iso(shipment.eta)

    parts = []
    if status:
        parts.append(status)
    elif fallback := _origin_fallback(shipment, lc):
        parts.append(fallback)
    if berth and status and "berth" not in status.lower():
        parts.append(f"Berth {berth}")

    return {
        "vessel_status": status,
        "vessel_location": raw_loc or status,
        "kpt_berth": berth,
        "on_port_date": on_port,
        "departure_date": departed,
        "eta": eta,
        "country_port": " · ".join(parts) if parts else None,
        "port_of_discharge": (shipment.port_of_discharge or "").strip() or None,
    }


def vessel_status_summary(shipments: list) -> dict:
    """Pick the most advanced status across all shipments on a vessel."""
    if not shipments:
        return {
            "vessel_status": None,
            "vessel_location": None,
            "kpt_berth": None,
            "on_port_date": None,
            "departure_date": None,
            "country_port": None,
        }
    resolved = [resolve_vessel_status(s) for s in shipments]

    def _rank(r):
        st = r.get("vessel_status") or ""
        key = _status_key(st) or ""
        return _STATUS_RANK.get(key, 0)

    best = max(resolved, key=_rank)
    # Earliest on-port / latest departure across shipments (conservative for the vessel)
    on_ports = [r["on_port_date"] for r in resolved if r.get("on_port_date")]
    departures = [r["departure_date"] for r in resolved if r.get("departure_date")]
    berths = [r["kpt_berth"] for r in resolved if r.get("kpt_berth")]
    return {
        **best,
        "on_port_date": min(on_ports) if on_ports else best.get("on_port_date"),
        "departure_date": max(departures) if departures else best.get("departure_date"),
        "kpt_berth": berths[0] if berths else best.get("kpt_berth"),
    }


def sync_bl_demurrage_from_departure(shipment: Shipment, departure_date: date) -> None:
    """When a vessel departs, demurrage clock starts from departure date (overwrite)."""
    if not departure_date:
        return
    for bl in (shipment.bill_of_ladings or []):
        if getattr(bl, "bl_type", None) == "CONTAINER":
            continue
        bl.demurrage_start_date = departure_date


def bulk_set_demurrage_start_for_shipments(
    db: Session,
    shipment_ids: list[int],
    start_date: date,
) -> int:
    """Bulk-overwrite demurrage_start_date on every coil BL under the given shipments."""
    if not shipment_ids or not start_date:
        return 0
    return (
        db.query(BillOfLading)
        .filter(
            BillOfLading.shipment_id.in_(shipment_ids),
            or_(BillOfLading.bl_type.is_(None), BillOfLading.bl_type != "CONTAINER"),
        )
        .update({BillOfLading.demurrage_start_date: start_date}, synchronize_session=False)
    )


def apply_port_status(
    shipment: Shipment,
    port_status: str,
    *,
    berth: Optional[str] = None,
    on_port_date: Optional[date] = None,
    departure_date: Optional[date] = None,
) -> None:
    """Apply a manual or bulk port-status update to one shipment."""
    status = (port_status or "").strip()
    if not status:
        return

    key = _status_key(status)
    b = (berth or "").strip() or None

    if key == "berth":
        shipment.kpt_berth = b or (status[5:].strip(" -·") if status.lower().startswith("berth") else None)
        shipment.vessel_location = (
            f"Berth {shipment.kpt_berth}".strip() if shipment.kpt_berth else "Berth"
        )
        if on_port_date:
            shipment.on_port_date = on_port_date
        return

    if key == "on port":
        shipment.vessel_location = "On port"
        if b:
            shipment.kpt_berth = b
        if on_port_date:
            shipment.on_port_date = on_port_date
        return

    if key in ("departed", "left port"):
        shipment.vessel_location = "Left Port" if key == "left port" else "Departed"
        if departure_date:
            shipment.departure_date = departure_date
            sync_bl_demurrage_from_departure(shipment, departure_date)
        return

    if key == "expected arrival":
        shipment.vessel_location = "Expected Arrival"
        return

    # Custom free-text
    shipment.vessel_location = status
    if b:
        shipment.kpt_berth = b
    if on_port_date:
        shipment.on_port_date = on_port_date
    if departure_date:
        shipment.departure_date = departure_date
        sync_bl_demurrage_from_departure(shipment, departure_date)

    # Auto-sync on_port_date as demurrage/detention start date on BLs if not explicitly set
    if shipment.on_port_date:
        for bl in (shipment.bill_of_ladings or []):
            if not bl.demurrage_start_date:
                bl.demurrage_start_date = shipment.on_port_date
            if not bl.detention_start_date:
                bl.detention_start_date = shipment.on_port_date
