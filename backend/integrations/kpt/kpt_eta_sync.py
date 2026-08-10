"""Sync shipment ETAs from KPT Expected Arrivals for eligible LCs."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from config.settings import settings
from models.database_models import LCMaster, Shipment
from integrations.kpt.kpt_eta_crawler import (
    KPTVesselArrival,
    crawl_expected_arrivals_sync,
    find_arrival_for_vessel,
    index_arrivals,
    normalize_vessel_name,
)
from modules.shipments.vessel_status_service import normalize_port_status

logger = logging.getLogger("uvicorn")


def _lc_base_date(lc: LCMaster) -> Optional[date]:
    if lc.import_date:
        return lc.import_date.date() if isinstance(lc.import_date, datetime) else lc.import_date
    return lc.lc_date


def eligible_vessel_targets(db: Session, min_lc_age_days: int) -> Dict[str, dict]:
    """Vessels on shipments whose parent LC is at least *min_lc_age_days* old."""
    cutoff = date.today() - timedelta(days=min_lc_age_days)
    rows = (
        db.query(Shipment)
        .options(joinedload(Shipment.lc))
        .filter(
            Shipment.is_deleted.is_(False),
            Shipment.vessel_name.isnot(None),
            Shipment.vessel_name != "",
        )
        .all()
    )
    targets: Dict[str, dict] = {}
    for s in rows:
        lc = s.lc
        if not lc:
            continue
        base = _lc_base_date(lc)
        if not base or base > cutoff:
            continue
        key = normalize_vessel_name(s.vessel_name)
        if not key:
            continue
        bucket = targets.setdefault(
            key,
            {"vessel_key": key, "display_name": (s.vessel_name or "").strip(), "shipment_ids": set()},
        )
        bucket["shipment_ids"].add(s.shipment_id)
        if len((s.vessel_name or "").strip()) > len(bucket["display_name"]):
            bucket["display_name"] = (s.vessel_name or "").strip()
    return targets


def _shipments_for_vessel_key(db: Session, vessel_key: str) -> List[Shipment]:
    q = db.query(Shipment).options(joinedload(Shipment.lc)).filter(Shipment.is_deleted.is_(False))
    core = (vessel_key or "").split(" ")[0]
    if core:
        q = q.filter(Shipment.vessel_name.ilike(f"%{core}%"))
    return [s for s in q.all() if normalize_vessel_name(s.vessel_name) == vessel_key]


def apply_arrival_to_shipments(
    db: Session,
    vessel_key: str,
    arrival: KPTVesselArrival,
) -> tuple[int, List[int]]:
    from integrations.kpt.kpt_document_alerts import stamp_kpt_eta

    ships = _shipments_for_vessel_key(db, vessel_key)
    if not ships:
        return 0, []
    port_status = normalize_port_status(f"KPT: {arrival.status}")
    updated_ids: List[int] = []
    for s in ships:
        s.eta = arrival.eta_date
        # Real port-authority tracking data outranks the auto-ETA formula — mark it so the
        # formula backs off until a human explicitly resets it (see Shipment.eta_source).
        s.eta_source = "WEBSITE"
        s.vessel_location = port_status
        s.vessel_status_source = "WEBSITE"
        s.vessel_status_updated_at = datetime.utcnow()
        if s.lc and s.lc.eta != arrival.eta_date:
            s.lc.eta = arrival.eta_date
        updated_ids.append(s.shipment_id)
    stamp_kpt_eta(db, ships)
    return len(ships), updated_ids


def run_kpt_eta_sync(
    db: Session,
    *,
    min_lc_age_days: int = 23,
    url: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    targets = eligible_vessel_targets(db, min_lc_age_days)
    summary = {
        "eligible_vessels": len(targets),
        "kpt_rows": 0,
        "matched": 0,
        "updated_shipments": 0,
        "not_found": [],
        "details": [],
        "dry_run": dry_run,
    }
    if not targets:
        logger.info("[KPT ETA sync] no eligible vessels (LC age rule)")
        return summary

    affected_ids: List[int] = []
    arrivals = crawl_expected_arrivals_sync(url) if url else crawl_expected_arrivals_sync()
    summary["kpt_rows"] = len(arrivals)
    idx = index_arrivals(arrivals)

    for key, meta in sorted(targets.items()):
        arrival = find_arrival_for_vessel(meta["display_name"], arrivals, index=idx)
        if not arrival:
            summary["not_found"].append(meta["display_name"])
            summary["details"].append(
                {"vessel": meta["display_name"], "status": "not_found_on_kpt"}
            )
            continue
        summary["matched"] += 1
        if dry_run:
            summary["details"].append(
                {
                    "vessel": meta["display_name"],
                    "status": "would_update",
                    "eta": arrival.eta_date.isoformat(),
                    "kpt_status": arrival.status,
                    "shipments": len(meta["shipment_ids"]),
                }
            )
            continue
        n, ids = apply_arrival_to_shipments(db, key, arrival)
        affected_ids.extend(ids)
        summary["updated_shipments"] += n
        summary["details"].append(
            {
                "vessel": meta["display_name"],
                "status": "updated",
                "eta": arrival.eta_date.isoformat(),
                "kpt_status": arrival.status,
                "shipments": n,
            }
        )

    if not dry_run:
        if affected_ids and settings.KPT_DOC_ALERTS_ENABLED:
            from integrations.kpt.kpt_document_alerts import scan_eta_document_alerts
            summary["doc_alerts"] = scan_eta_document_alerts(db, affected_ids)
        db.commit()
    logger.info(
        "[KPT ETA sync] eligible=%s matched=%s updated_shipments=%s not_found=%s",
        summary["eligible_vessels"],
        summary["matched"],
        summary["updated_shipments"],
        len(summary["not_found"]),
    )
    return summary


def smoke_test_crawler(vessel_name: Optional[str] = None, url: Optional[str] = None) -> dict:
    arrivals = crawl_expected_arrivals_sync(url) if url else crawl_expected_arrivals_sync()
    out = {
        "ok": True,
        "vessel_count": len(arrivals),
        "sample": [
            {
                "name": a.name,
                "eta": a.eta_text,
                "eta_date": a.eta_date.isoformat(),
                "status": a.status,
                "agent": a.agent,
            }
            for a in arrivals[:5]
        ],
    }
    if vessel_name:
        hit = find_arrival_for_vessel(vessel_name, arrivals)
        out["lookup"] = {
            "query": vessel_name,
            "found": bool(hit),
            "match": None
            if not hit
            else {
                "name": hit.name,
                "eta": hit.eta_text,
                "eta_date": hit.eta_date.isoformat(),
                "status": hit.status,
            },
        }
    return out
