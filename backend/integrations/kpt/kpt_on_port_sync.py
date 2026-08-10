"""Sync shipment port status from KPT Ships On Port for eligible LCs."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import settings
from integrations.kpt.kpt_eta_sync import eligible_vessel_targets, _shipments_for_vessel_key
from integrations.kpt.kpt_on_port_crawler import (
    KPTVesselOnPort,
    crawl_ships_on_port_sync,
    find_on_port_vessel,
    index_on_port_vessels,
)

logger = logging.getLogger("uvicorn")

ON_PORT_STATUS = "On port"


def apply_on_port_to_shipments(
    db: Session,
    vessel_key: str,
    row: KPTVesselOnPort,
) -> tuple[int, list[int]]:
    from integrations.kpt.kpt_document_alerts import stamp_kpt_on_port

    ships = _shipments_for_vessel_key(db, vessel_key)
    if not ships:
        return 0, []
    updated_ids = []
    for s in ships:
        berth = (row.berth or "").strip() or None
        if berth:
            s.kpt_berth = berth
            s.vessel_location = f"Berth {berth}"
        else:
            s.vessel_location = ON_PORT_STATUS
        s.vessel_status_source = "WEBSITE"
        s.vessel_status_updated_at = datetime.utcnow()
        if row.arrival_at:
            s.on_port_date = row.arrival_at.date() if hasattr(row.arrival_at, "date") else row.arrival_at
        updated_ids.append(s.shipment_id)
    stamp_kpt_on_port(db, ships)
    return len(ships), updated_ids


def run_kpt_on_port_sync(
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
        logger.info("[KPT on-port sync] no eligible vessels (LC age rule)")
        return summary

    affected_ids: list[int] = []
    rows = crawl_ships_on_port_sync(url) if url else crawl_ships_on_port_sync()
    summary["kpt_rows"] = len(rows)
    idx = index_on_port_vessels(rows)

    for key, meta in sorted(targets.items()):
        hit = find_on_port_vessel(meta["display_name"], rows, index=idx)
        if not hit:
            summary["not_found"].append(meta["display_name"])
            summary["details"].append(
                {"vessel": meta["display_name"], "status": "not_on_port"}
            )
            continue
        summary["matched"] += 1
        if dry_run:
            summary["details"].append(
                {
                    "vessel": meta["display_name"],
                    "status": "would_update",
                    "port_status": ON_PORT_STATUS,
                    "berth": hit.berth,
                    "arrival": hit.arrival_text,
                    "shipments": len(meta["shipment_ids"]),
                }
            )
            continue
        n, ids = apply_on_port_to_shipments(db, key, hit)
        affected_ids.extend(ids)
        summary["updated_shipments"] += n
        summary["details"].append(
            {
                "vessel": meta["display_name"],
                "status": "updated",
                "port_status": ON_PORT_STATUS,
                "berth": hit.berth,
                "arrival": hit.arrival_text,
                "shipments": n,
            }
        )

    if not dry_run:
        if affected_ids and settings.KPT_DOC_ALERTS_ENABLED:
            from integrations.kpt.kpt_document_alerts import scan_on_port_gd_alerts
            summary["doc_alerts"] = scan_on_port_gd_alerts(db, affected_ids)
        db.commit()
    logger.info(
        "[KPT on-port sync] eligible=%s matched=%s updated_shipments=%s not_found=%s",
        summary["eligible_vessels"],
        summary["matched"],
        summary["updated_shipments"],
        len(summary["not_found"]),
    )
    return summary


def smoke_test_on_port_crawler(vessel_name: Optional[str] = None, url: Optional[str] = None) -> dict:
    rows = crawl_ships_on_port_sync(url) if url else crawl_ships_on_port_sync()
    out = {
        "ok": True,
        "vessel_count": len(rows),
        "sample": [
            {
                "name": r.name,
                "berth": r.berth,
                "cargo": r.cargo_type,
                "arrival": r.arrival_text,
                "agent": r.agent,
            }
            for r in rows[:5]
        ],
    }
    if vessel_name:
        hit = find_on_port_vessel(vessel_name, rows)
        out["lookup"] = {
            "query": vessel_name,
            "found": bool(hit),
            "match": None
            if not hit
            else {
                "name": hit.name,
                "berth": hit.berth,
                "arrival": hit.arrival_text,
            },
        }
    return out
