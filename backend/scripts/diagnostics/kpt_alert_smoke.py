"""
Smoke test KPT document alert rules (ETA docs, on-port GD, departure pickup).

Creates temporary rows, exercises alert scan + acknowledge + re-alert + resolve, then cleans up.

  python kpt_alert_smoke.py
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import SessionLocal
from models.database_models import (
    BillOfLading,
    CommercialInvoice,
    GoodsDeclaration,
    LCMaster,
    PackingList,
    Shipment,
    SystemAlert,
)
from integrations.kpt.kpt_document_alerts import (
    ALERT_ETA_DOCS,
    ALERT_ON_PORT_GD,
    ALERT_PORT_PICKUP,
    run_kpt_document_alert_scan,
    scan_departure_pickup_alerts,
    scan_eta_document_alerts,
    scan_on_port_gd_alerts,
)


def _active_alerts(db, shipment_id: int, alert_type: str):
    return (
        db.query(SystemAlert)
        .filter(
            SystemAlert.shipment_id == shipment_id,
            SystemAlert.alert_type == alert_type,
            SystemAlert.status == "ACTIVE",
        )
        .all()
    )


def _ack_all(db, shipment_id: int, alert_type: str):
    rows = _active_alerts(db, shipment_id, alert_type)
    for a in rows:
        a.status = "ACKNOWLEDGED"
        a.acknowledged_at = datetime.utcnow()
        a.acknowledged_by = 1
    db.commit()
    return len(rows)


def run_smoke() -> dict:
    db = SessionLocal()
    lc_id = shipment_id = bl_id = None
    results = {"ok": True, "steps": []}

    def step(name, passed, detail=None):
        results["steps"].append({"step": name, "ok": passed, "detail": detail})
        if not passed:
            results["ok"] = False

    try:
        old = date.today() - timedelta(days=30)
        lc = LCMaster(
            lc_number=f"KPT-ALERT-{int(datetime.now().timestamp())}",
            lc_date=old,
            import_date=datetime.combine(old, datetime.min.time()),
            monitoring_expiry=date.today() + timedelta(days=40),
            status="OPEN",
        )
        db.add(lc)
        db.flush()
        lc_id = lc.lc_id

        ship = Shipment(
            lc_id=lc_id,
            shipment_ref="KPT-ALERT-SMOKE",
            vessel_name="Yu Tong",
            status="PENDING",
            eta=date.today() + timedelta(days=2),
            kpt_eta_at=datetime.utcnow(),
        )
        db.add(ship)
        db.flush()
        shipment_id = ship.shipment_id

        bl = BillOfLading(shipment_id=shipment_id, lc_id=lc_id, bl_number="BL-SMOKE-001")
        db.add(bl)
        db.flush()
        bl_id = bl.bl_id
        db.commit()

        # 1) ETA docs missing -> alert
        r1 = scan_eta_document_alerts(db, [shipment_id])
        db.commit()
        eta_alerts = _active_alerts(db, shipment_id, ALERT_ETA_DOCS)
        step("eta_missing_docs_alert", len(eta_alerts) == 1 and r1["created"] == 1, {"created": r1["created"]})

        # 2) Acknowledge -> no immediate re-alert
        _ack_all(db, shipment_id, ALERT_ETA_DOCS)
        r1b = scan_eta_document_alerts(db, [shipment_id])
        db.commit()
        step("eta_ack_suppresses_realert", r1b["created"] == 0, r1b)

        # 3) Ack stale 25h -> re-alert
        stale = (
            db.query(SystemAlert)
            .filter(SystemAlert.shipment_id == shipment_id, SystemAlert.alert_type == ALERT_ETA_DOCS)
            .order_by(SystemAlert.alert_id.desc())
            .first()
        )
        stale.acknowledged_at = datetime.utcnow() - timedelta(hours=25)
        db.commit()
        r1c = scan_eta_document_alerts(db, [shipment_id])
        db.commit()
        step("eta_realert_after_24h", r1c["created"] == 1, r1c)

        # 4) Upload docs -> resolved
        ship = db.query(Shipment).get(shipment_id)
        db.add(CommercialInvoice(shipment_id=shipment_id, lc_id=lc_id, document_path="/tmp/smoke-inv.pdf"))
        db.add(PackingList(shipment_id=shipment_id, document_path="/tmp/smoke-pkg.pdf"))
        bl.document_path = "/tmp/smoke-bl.pdf"
        db.commit()
        r1d = scan_eta_document_alerts(db, [shipment_id])
        db.commit()
        step("eta_resolved_when_uploaded", r1d["resolved"] >= 1 and not _active_alerts(db, shipment_id, ALERT_ETA_DOCS), r1d)

        # 5) On port GD missing
        ship = db.query(Shipment).get(shipment_id)
        ship.vessel_location = "On port"
        ship.kpt_on_port_at = datetime.utcnow()
        db.commit()
        r2 = scan_on_port_gd_alerts(db, [shipment_id])
        db.commit()
        gd_alerts = _active_alerts(db, shipment_id, ALERT_ON_PORT_GD)
        step("on_port_gd_alert", len(gd_alerts) == 1 and r2["created"] == 1, r2)

        db.add(
            GoodsDeclaration(
                shipment_id=shipment_id,
                lc_id=lc_id,
                document_path="/tmp/smoke-gd.pdf",
                gd_view_uploaded=True,
            )
        )
        db.commit()
        r2b = scan_on_port_gd_alerts(db, [shipment_id])
        db.commit()
        step("on_port_gd_resolved", r2b["resolved"] >= 1 and not _active_alerts(db, shipment_id, ALERT_ON_PORT_GD), r2b)

        # 6) Departure pickup after 3 days
        ship = db.query(Shipment).get(shipment_id)
        ship.vessel_location = "Departed"
        ship.kpt_departed_at = datetime.utcnow() - timedelta(days=4)
        db.commit()
        r3 = scan_departure_pickup_alerts(db, [shipment_id], pickup_days=3)
        db.commit()
        pickup_alerts = _active_alerts(db, shipment_id, ALERT_PORT_PICKUP)
        step("departure_pickup_alert", len(pickup_alerts) >= 1 and r3["created"] >= 1, r3)

        ship = db.query(Shipment).get(shipment_id)
        ship.delivery_date = date.today()
        db.commit()
        r3b = scan_departure_pickup_alerts(db, [shipment_id], pickup_days=3)
        db.commit()
        step("pickup_resolved_on_delivery", r3b["resolved"] >= 1 and not _active_alerts(db, shipment_id, ALERT_PORT_PICKUP), r3b)

        full = run_kpt_document_alert_scan(db)
        db.commit()
        step("full_scan_runs", isinstance(full.get("total_created"), int), full)

        return results
    finally:
        if shipment_id:
            db.query(SystemAlert).filter(SystemAlert.shipment_id == shipment_id).delete()
            db.query(GoodsDeclaration).filter(GoodsDeclaration.shipment_id == shipment_id).delete()
            db.query(PackingList).filter(PackingList.shipment_id == shipment_id).delete()
            db.query(CommercialInvoice).filter(CommercialInvoice.shipment_id == shipment_id).delete()
            db.query(BillOfLading).filter(BillOfLading.shipment_id == shipment_id).delete()
            db.query(Shipment).filter(Shipment.shipment_id == shipment_id).delete()
        if lc_id:
            db.query(LCMaster).filter(LCMaster.lc_id == lc_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    out = run_smoke()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out.get("ok") else 1)
