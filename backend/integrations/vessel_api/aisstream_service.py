"""AISStream WebSocket fallback — Karachi / Northern Arabian Sea focused."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import websockets

from config.settings import settings

logger = logging.getLogger("uvicorn")

# AIS destination strings commonly used for Karachi / Port Qasim traffic
_KARACHI_DEST_TOKENS = (
    "KARACHI",
    "KHI",
    "PKKHI",
    "PKBQM",
    "QASIM",
    "BIN QASIM",
    "PORT QASIM",
    "KEAMARI",
    "KPT",
)


class AISStreamError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def is_karachi_destination(destination: Optional[str]) -> bool:
    """True if AIS destination text points at Karachi / Port Qasim."""
    text = (destination or "").strip().upper()
    if not text:
        return False
    return any(token in text for token in _KARACHI_DEST_TOKENS)


def _name_matches(query: str, ship_name: str) -> bool:
    q = (query or "").strip().upper()
    s = (ship_name or "").strip().upper()
    if not q or not s:
        return False
    return q in s or s in q


def _mmsi_matches(query: str, mmsi: Any) -> bool:
    if not query.isdigit() or len(query) != 9:
        return False
    return str(mmsi or "") == query


def _format_eta(eta_obj: Optional[dict]) -> Optional[str]:
    if not isinstance(eta_obj, dict):
        return None
    month = eta_obj.get("Month")
    day = eta_obj.get("Day")
    hour = eta_obj.get("Hour")
    minute = eta_obj.get("Minute")
    if not month or not day:
        return None
    try:
        return (
            f"{int(month):02d}/{int(day):02d} "
            f"{int(hour or 0):02d}:{int(minute or 0):02d} UTC"
        )
    except (TypeError, ValueError):
        return None


def _karachi_boxes() -> list:
    boxes = settings.AISSTREAM_BOUNDING_BOXES or []
    if boxes:
        return boxes
    return [[[24.50, 66.70], [25.20, 67.60]], [[20.00, 60.00], [26.50, 71.00]]]


async def track_via_aisstream(query: str, timeout_seconds: Optional[int] = None) -> dict:
    """Listen near Karachi until a matching vessel reports position (+ optional static)."""
    if not settings.AISSTREAM_API_KEY:
        raise AISStreamError(
            "AISStream is not configured. Set AISSTREAM_API_KEY in backend/.env.",
            status_code=503,
        )

    search = (query or "").strip()
    if not search:
        raise AISStreamError("Vessel name or MMSI is required.", 400)

    timeout = timeout_seconds or settings.AISSTREAM_TIMEOUT_SECONDS
    vessel: Dict[str, Any] = {}
    position: Dict[str, Any] = {}
    eta: Dict[str, Any] = {}
    got_position = False
    karachi_dest = False

    try:
        async with websockets.connect(
            settings.AISSTREAM_URL,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "APIKey": settings.AISSTREAM_API_KEY,
                        "BoundingBoxes": _karachi_boxes(),
                        "FilterMessageTypes": [
                            "PositionReport",
                            "ShipStaticData",
                            "ExtendedClassBPositionReport",
                        ],
                    }
                )
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            # After first position hit, wait briefly for ShipStaticData (IMO/callsign/ETA).
            static_grace_deadline: Optional[float] = None

            while True:
                now = loop.time()
                if now >= deadline:
                    break
                if got_position and static_grace_deadline and now >= static_grace_deadline:
                    break
                wait_for = min(
                    deadline - now,
                    (static_grace_deadline - now) if static_grace_deadline else timeout,
                )
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(wait_for, 0.1))
                except asyncio.TimeoutError:
                    break

                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                meta = msg.get("MetaData") or {}
                msg_type = msg.get("MessageType")
                body = msg.get("Message") or {}

                ship_name = (meta.get("ShipName") or "").strip()
                if not ship_name and msg_type == "ShipStaticData":
                    ship_name = ((body.get("ShipStaticData") or {}).get("Name") or "").strip()

                mmsi = meta.get("MMSI")
                matched = _mmsi_matches(search, mmsi) or _name_matches(search, ship_name)
                if not matched:
                    continue

                if msg_type in ("PositionReport", "ExtendedClassBPositionReport"):
                    pos = body.get("PositionReport") or body.get("ExtendedClassBPositionReport") or {}
                    lat = meta.get("latitude")
                    if lat is None:
                        lat = pos.get("Latitude")
                    lon = meta.get("longitude")
                    if lon is None:
                        lon = pos.get("Longitude")
                    if lat is None or lon is None:
                        continue
                    position = {
                        "mmsi": mmsi or pos.get("UserID"),
                        "imo": vessel.get("imo"),
                        "vessel_name": ship_name or vessel.get("name") or search,
                        "latitude": float(lat),
                        "longitude": float(lon),
                        "timestamp": meta.get("time_utc")
                        or datetime.now(timezone.utc).isoformat(),
                        "sog": pos.get("Sog"),
                        "cog": pos.get("Cog"),
                        "heading": pos.get("TrueHeading"),
                        "nav_status": pos.get("NavigationalStatus"),
                    }
                    vessel.setdefault("name", ship_name or search.upper())
                    vessel.setdefault("mmsi", position["mmsi"])
                    got_position = True
                    if vessel.get("imo") or vessel.get("call_sign"):
                        break
                    if static_grace_deadline is None:
                        static_grace_deadline = loop.time() + 8

                elif msg_type == "ShipStaticData":
                    static = body.get("ShipStaticData") or {}
                    dim = static.get("Dimension") or {}
                    vessel.update(
                        {
                            "name": (static.get("Name") or ship_name or search).strip(),
                            "mmsi": mmsi or static.get("UserID"),
                            "imo": static.get("ImoNumber"),
                            "call_sign": (static.get("CallSign") or "").strip() or None,
                            "vessel_type": static.get("Type"),
                            "length": (dim.get("A") or 0) + (dim.get("B") or 0) or None,
                            "breadth": (dim.get("C") or 0) + (dim.get("D") or 0) or None,
                            "draft": static.get("MaximumStaticDraught"),
                        }
                    )
                    dest = (static.get("Destination") or "").strip()
                    eta_text = _format_eta(static.get("Eta"))
                    karachi_dest = is_karachi_destination(dest)
                    if dest or eta_text:
                        eta = {
                            "mmsi": vessel.get("mmsi"),
                            "imo": vessel.get("imo"),
                            "vessel_name": vessel.get("name"),
                            "destination": dest or None,
                            "eta": eta_text,
                            "draught": static.get("MaximumStaticDraught"),
                            "timestamp": meta.get("time_utc"),
                        }
                    if position:
                        position["imo"] = vessel.get("imo")
                        position["vessel_name"] = vessel.get("name") or position.get("vessel_name")
                    if got_position:
                        break

    except AISStreamError:
        raise
    except Exception as exc:
        logger.warning("[AISStream] connection failed: %s", exc)
        raise AISStreamError(f"AISStream connection failed: {exc}", 502) from exc

    if not got_position:
        raise AISStreamError(
            f'"{search}" not seen near Karachi / Arabian Sea approaches within {timeout}s '
            f"(may still be far offshore, offline, or only on VesselAPI coverage).",
            404,
        )

    # Physically in Karachi AIS coverage ⇒ relevant for PK importers even if dest already updated
    karachi_relevant = True

    return {
        "query": search,
        "match_count": 1,
        "matches": [vessel],
        "vessel": vessel,
        "position": position,
        "eta": eta,
        "quota": {"remaining": None, "source": "aisstream"},
        "provider": "aisstream",
        "karachi_relevant": karachi_relevant,
        "karachi_destination": karachi_dest,
        "coverage": "karachi_approaches",
    }
