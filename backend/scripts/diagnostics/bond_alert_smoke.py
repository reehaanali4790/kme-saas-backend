"""
Smoke test the into-bond 180-day clock: alerts, multi-EB settlement, retroactive
clearance and the penalty counter.

Creates a temporary LC/shipment/IB GD, drives the clock by moving the IB filing date
(so "today" stays real), then cleans everything up.

  python bond_alert_smoke.py
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import SessionLocal
from models.database_models import (
    ExBondEntry,
    GoodsDeclaration,
    LCMaster,
    Shipment,
    SystemAlert,
)
from modules.weboc.helpers.bond_alerts import (
    ALERT_BOND_DUE,
    ALERT_BOND_OVERDUE,
    ALERT_BOND_PENALTY_DUE,
    ALERT_BOND_PENALTY_VOID,
    scan_bond_alerts,
)
from modules.weboc.helpers.weboc_service import INTO_BOND_DAYS, BOND_WARN_LEAD, bond_summary

BONDED_MT = Decimal("100.000")


def _active(db, gd_id: int, alert_type: str):
    return (
        db.query(SystemAlert)
        .filter(
            SystemAlert.entity_id == gd_id,
            SystemAlert.entity_type == "GD",
            SystemAlert.alert_type == alert_type,
            SystemAlert.status == "ACTIVE",
        )
        .all()
    )


def _ack_all(db, gd_id: int, alert_type: str, hours_ago: float = 0):
    """Acknowledge the family and age the acknowledgement by `hours_ago`.

    Must cover already-ACKNOWLEDGED rows too, not just ACTIVE ones: the cooldown is
    judged on the latest open alert in the family, so ageing only the ACTIVE rows would
    leave a fresh acknowledgement sitting in front of it and hold the alert quiet.
    """
    rows = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.entity_id == gd_id,
            SystemAlert.entity_type == "GD",
            SystemAlert.alert_type == alert_type,
            SystemAlert.status.in_(["ACTIVE", "ACKNOWLEDGED"]),
        )
        .all()
    )
    for a in rows:
        a.status = "ACKNOWLEDGED"
        a.acknowledged_at = datetime.utcnow() - timedelta(hours=hours_ago)
        a.acknowledged_by = 1
    db.commit()
    return len(rows)


def _set_clock(db, gd: GoodsDeclaration, days_elapsed: int):
    """Put the IB filing date `days_elapsed` days in the past — day N of 180."""
    gd.filing_date = date.today() - timedelta(days=days_elapsed)
    db.commit()


def _add_eb(db, gd: GoodsDeclaration, qty, days_ago: int, number: str):
    e = ExBondEntry(
        into_bond_gd_id=gd.gd_id,
        shipment_id=gd.shipment_id,
        gd_number=number,
        ex_bond_date=date.today() - timedelta(days=days_ago),
        quantity_mt=Decimal(str(qty)),
    )
    db.add(e)
    db.commit()
    return e


def run_smoke() -> dict:
    db = SessionLocal()
    lc_id = shipment_id = gd_id = None
    results = {"ok": True, "steps": []}

    def step(name, passed, detail=None):
        results["steps"].append({"step": name, "ok": bool(passed), "detail": detail})
        if not passed:
            results["ok"] = False

    try:
        opened = date.today() - timedelta(days=200)
        lc = LCMaster(
            lc_number=f"BOND-SMOKE-{int(datetime.now().timestamp())}",
            lc_date=opened,
            import_date=datetime.combine(opened, datetime.min.time()),
            monitoring_expiry=date.today() + timedelta(days=40),
            status="OPEN",
        )
        db.add(lc)
        db.flush()
        lc_id = lc.lc_id

        ship = Shipment(lc_id=lc_id, shipment_ref="BOND-SMOKE", vessel_name="Bond Runner",
                        status="PENDING")
        db.add(ship)
        db.flush()
        shipment_id = ship.shipment_id

        gd = GoodsDeclaration(
            shipment_id=shipment_id, lc_id=lc_id,
            gd_number="KEWB-IB-9001", gd_type="INTO_BOND",
            gross_weight_mt=BONDED_MT, bonded_qty_mt=BONDED_MT,
            into_bond_gd_uploaded=True,
        )
        db.add(gd)
        db.flush()
        gd_id = gd.gd_id
        db.commit()

        # ================= SCENARIO 1: the 20-day alert =================
        # Day 150 of 180 -> 30 days left. Outside the 20-day lead: silence.
        _set_clock(db, gd, 150)
        b = bond_summary(gd, db)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("day150_no_alert_yet",
             b["state"] == "OK" and b["days_remaining"] == 30 and not _active(db, gd_id, ALERT_BOND_DUE),
             {"state": b["state"], "days_remaining": b["days_remaining"], "scan": r})

        # Day 160 of 180 -> exactly 20 days left. The alert opens.
        _set_clock(db, gd, 180 - BOND_WARN_LEAD)
        b = bond_summary(gd, db)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("day160_alert_fires_at_20_days_left",
             b["state"] == "DUE_SOON" and b["days_remaining"] == BOND_WARN_LEAD
             and len(_active(db, gd_id, ALERT_BOND_DUE)) == 1 and r["created"] == 1,
             {"state": b["state"], "days_remaining": b["days_remaining"], "scan": r})

        # Re-scan while still ACTIVE -> no duplicate.
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("active_alert_not_duplicated",
             r["created"] == 0 and len(_active(db, gd_id, ALERT_BOND_DUE)) == 1, r)

        # Acknowledge -> quiet inside the cooldown.
        _ack_all(db, gd_id, ALERT_BOND_DUE, hours_ago=0)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("ack_suppresses_realert", r["created"] == 0, r)

        # Acknowledged 25h ago -> re-alerts (daily until cleared).
        _ack_all(db, gd_id, ALERT_BOND_DUE, hours_ago=25)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("realert_daily_after_24h_ack",
             r["created"] == 1 and len(_active(db, gd_id, ALERT_BOND_DUE)) == 1, r)

        # ============= SCENARIO 2: multiple EBs settling the amount =============
        # Three liftings: 40 + 35 = 75 of 100 leaves the bond open...
        _add_eb(db, gd, 40, days_ago=15, number="KEWB-EX-1")
        _add_eb(db, gd, 35, days_ago=10, number="KEWB-EX-2")
        b = bond_summary(gd, db)
        step("multi_eb_partial_not_settled",
             not b["is_weight_settled"] and abs(b["lifted_qty_mt"] - 75.0) < 1e-6
             and abs(b["remaining_qty_mt"] - 25.0) < 1e-6 and b["ex_bond_count"] == 2,
             {"lifted": b["lifted_qty_mt"], "remaining": b["remaining_qty_mt"],
              "settled": b["is_weight_settled"]})

        # ...and the alert is still running while 25 MT sits in bond.
        _ack_all(db, gd_id, ALERT_BOND_DUE, hours_ago=25)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("alert_persists_while_partially_lifted", r["created"] == 1, r)

        # The third EB settles it: 75 + 24 = 99 of 100. 1 MT remains = 1% -> inside the
        # 1-2% tolerance, so the bond is settled without lifting the last kilo.
        _add_eb(db, gd, 24, days_ago=5, number="KEWB-EX-3")
        b = bond_summary(gd, db)
        step("multi_eb_settles_within_tolerance",
             b["is_weight_settled"] and b["state"] == "CLEARED"
             and abs(b["remaining_qty_mt"] - 1.0) < 1e-6 and b["ex_bond_count"] == 3,
             {"lifted": b["lifted_qty_mt"], "remaining": b["remaining_qty_mt"],
              "tolerance_mt": b["tolerance_mt"], "state": b["state"],
              "settlement_date": b["settlement_date"]})

        # The settling EB is the third one — the timer stops on ITS date, not today.
        step("settlement_date_is_settling_eb",
             b["settlement_date"] == (date.today() - timedelta(days=5)).isoformat()
             and not b["settled_late"],
             {"settlement_date": b["settlement_date"], "settled_late": b["settled_late"]})

        # Settled -> every alert for this GD resolves.
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("settled_resolves_alerts",
             r["resolved"] >= 1 and not _active(db, gd_id, ALERT_BOND_DUE), r)

        # ========== SCENARIO 3: forgotten EB -> alert -> back-dated EB voids it ==========
        # Wipe the liftings and push the clock to day 200: 20 days past the deadline.
        db.query(ExBondEntry).filter(ExBondEntry.into_bond_gd_id == gd_id).delete()
        db.query(SystemAlert).filter(SystemAlert.entity_id == gd_id,
                                     SystemAlert.entity_type == "GD").delete()
        db.commit()
        _set_clock(db, gd, 200)
        b = bond_summary(gd, db)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("day200_overdue_alert_and_penalty_applies",
             b["state"] == "PASSED" and b["overdue_days"] == 20 and b["penalty_applies"]
             and len(_active(db, gd_id, ALERT_BOND_OVERDUE)) == 1,
             {"state": b["state"], "overdue_days": b["overdue_days"],
              "penalty_applies": b["penalty_applies"], "scan": r})

        # Overdue alert re-alerts daily too.
        _ack_all(db, gd_id, ALERT_BOND_OVERDUE, hours_ago=25)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("overdue_realerts_daily", r["created"] == 1, r)

        # The user records a penalty against what looks like a genuine breach.
        gd.bond_penalty_pkr = Decimal("500000")
        gd.bond_penalty_source = "MANUAL"
        gd.bond_penalty_reason = "Assumed breach — EB not yet received from clearing agent"
        gd.bond_penalty_days = 20
        gd.bond_penalty_recorded_at = datetime.utcnow()
        db.commit()
        b = bond_summary(gd, db)
        step("penalty_recorded_on_assumed_breach",
             b["penalty_recorded"] and b["penalty_pkr"] == 500000.0 and not b["penalty_stale"],
             {"penalty_pkr": b["penalty_pkr"], "stale": b["penalty_stale"]})

        # NOW the forgotten EB turns up — dated day 170, i.e. 10 days INSIDE the window.
        # It settles the whole bond, so the breach never actually happened.
        _add_eb(db, gd, 99, days_ago=200 - 170, number="KEWB-EX-LATE-ENTRY")
        b = bond_summary(gd, db)
        step("backdated_eb_settles_within_window",
             b["is_weight_settled"] and b["state"] == "CLEARED" and not b["settled_late"]
             and b["settlement_date"] == (gd.filing_date + timedelta(days=170)).isoformat(),
             {"state": b["state"], "settlement_date": b["settlement_date"],
              "deadline": b["deadline"], "settled_late": b["settled_late"]})

        # The recorded penalty is now provably wrong -> flagged stale, then voided.
        step("penalty_flagged_stale", b["penalty_stale"] and not b["penalty_applies"],
             {"stale": b["penalty_stale"], "applies": b["penalty_applies"]})

        r = scan_bond_alerts(db, [gd_id]); db.commit()
        db.refresh(gd)
        b = bond_summary(gd, db)
        step("penalty_voided_and_alerts_cleared",
             r["penalties_voided"] == 1 and gd.bond_penalty_pkr is None
             and not _active(db, gd_id, ALERT_BOND_OVERDUE)
             and not _active(db, gd_id, ALERT_BOND_PENALTY_DUE)
             and len(_active(db, gd_id, ALERT_BOND_PENALTY_VOID)) == 1,
             {"scan": r, "penalty_pkr": b["penalty_pkr"],
              "reason": gd.bond_penalty_reason})

        # ========== SCENARIO 4: genuine late settlement -> penalty stands ==========
        db.query(ExBondEntry).filter(ExBondEntry.into_bond_gd_id == gd_id).delete()
        db.query(SystemAlert).filter(SystemAlert.entity_id == gd_id,
                                     SystemAlert.entity_type == "GD").delete()
        gd.bond_penalty_pkr = None
        gd.bond_penalty_source = None
        gd.bond_penalty_reason = None
        gd.bond_penalty_days = None
        db.commit()

        # Clock at day 200; the settling EB is dated day 190 — 10 days PAST the deadline.
        _add_eb(db, gd, 99, days_ago=200 - 190, number="KEWB-EX-LATE")
        b = bond_summary(gd, db)
        step("late_eb_clears_bond_but_is_late",
             b["is_weight_settled"] and b["state"] == "CLEARED_LATE"
             and b["settled_late"] and b["settled_late_by_days"] == 10
             and b["penalty_applies"] and not b["penalty_recorded"],
             {"state": b["state"], "settled_late_by_days": b["settled_late_by_days"],
              "settlement_date": b["settlement_date"], "deadline": b["deadline"]})

        # Bond is closed, so the overdue chase stops — but the penalty chase starts.
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("late_settlement_raises_penalty_due",
             len(_active(db, gd_id, ALERT_BOND_PENALTY_DUE)) == 1
             and not _active(db, gd_id, ALERT_BOND_OVERDUE), r)

        # It re-alerts daily until the amount is in.
        _ack_all(db, gd_id, ALERT_BOND_PENALTY_DUE, hours_ago=25)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("penalty_due_realerts_daily", r["created"] == 1, r)

        # Penalty read off the document -> recorded -> the chase stops for good.
        gd.bond_penalty_pkr = Decimal("275000")
        gd.bond_penalty_source = "DOCUMENT"
        gd.bond_penalty_reason = "Surcharge on Ex-Bond GD KEWB-EX-LATE (10 days late)"
        gd.bond_penalty_days = 10
        gd.bond_penalty_recorded_at = datetime.utcnow()
        db.commit()
        b = bond_summary(gd, db)
        r = scan_bond_alerts(db, [gd_id]); db.commit()
        step("penalty_from_document_stops_chase",
             b["penalty_recorded"] and b["penalty_pkr"] == 275000.0
             and b["penalty_source"] == "DOCUMENT" and not b["penalty_stale"]
             and b["state"] == "CLEARED_LATE"
             and not _active(db, gd_id, ALERT_BOND_PENALTY_DUE),
             {"penalty_pkr": b["penalty_pkr"], "source": b["penalty_source"],
              "state": b["state"], "scan": r})

        # A genuine late settlement keeps its penalty — it is NOT voided.
        db.refresh(gd)
        step("genuine_penalty_survives_rescan",
             gd.bond_penalty_pkr == Decimal("275000.00") and r["penalties_voided"] == 0,
             {"penalty_pkr": str(gd.bond_penalty_pkr), "voided": r["penalties_voided"]})

        return results
    finally:
        # A failed step can leave the session in a rolled-back transaction; cleanup
        # still has to run or the smoke rows leak into the real tables.
        db.rollback()
        if gd_id:
            db.query(SystemAlert).filter(SystemAlert.entity_id == gd_id,
                                         SystemAlert.entity_type == "GD").delete()
            db.query(ExBondEntry).filter(ExBondEntry.into_bond_gd_id == gd_id).delete()
            db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).delete()
        if shipment_id:
            db.query(SystemAlert).filter(SystemAlert.shipment_id == shipment_id).delete()
            db.query(Shipment).filter(Shipment.shipment_id == shipment_id).delete()
        if lc_id:
            db.query(SystemAlert).filter(SystemAlert.lc_id == lc_id).delete()
            db.query(LCMaster).filter(LCMaster.lc_id == lc_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    out = run_smoke()
    print(json.dumps(out, indent=2, default=str))
    passed = sum(1 for s in out["steps"] if s["ok"])
    print(f"\n{passed}/{len(out['steps'])} steps passed")
    sys.exit(0 if out.get("ok") else 1)
