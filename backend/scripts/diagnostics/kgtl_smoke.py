"""
Smoke test the KGTL weighbridge reconciliation: per-vehicle rows, the own-vs-KGTL
difference, totals, partial rows, and the "GD is closed" gate.

  python kgtl_smoke.py
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.weboc.gd_service import gd_is_closed
from config.database import SessionLocal
from models.database_models import GdKgtlWeighment, GoodsDeclaration, LCMaster, Shipment
from modules.weboc.helpers.kgtl_service import kgtl_summary, row_difference_kg


def _add(db, gd, vehicle, own, kgtl, days_ago=0, own_coils=None, kgtl_coils=None):
    r = GdKgtlWeighment(
        gd_id=gd.gd_id,
        vehicle_number=vehicle,
        weigh_date=date.today() - timedelta(days=days_ago),
        own_weight_kg=None if own is None else Decimal(str(own)),
        kgtl_weight_kg=None if kgtl is None else Decimal(str(kgtl)),
        own_coils=own_coils,
        kgtl_coils=kgtl_coils,
    )
    db.add(r)
    db.commit()
    return r


def run_smoke() -> dict:
    db = SessionLocal()
    lc_id = shipment_id = gd_id = None
    results = {"ok": True, "steps": []}

    def step(name, passed, detail=None):
        results["steps"].append({"step": name, "ok": bool(passed), "detail": detail})
        if not passed:
            results["ok"] = False

    try:
        opened = date.today() - timedelta(days=60)
        lc = LCMaster(
            lc_number=f"KGTL-SMOKE-{int(datetime.now().timestamp())}",
            lc_date=opened,
            import_date=datetime.combine(opened, datetime.min.time()),
            monitoring_expiry=date.today() + timedelta(days=40),
            status="OPEN",
        )
        db.add(lc); db.flush()
        lc_id = lc.lc_id

        ship = Shipment(lc_id=lc_id, shipment_ref="KGTL-SMOKE", status="PENDING")
        db.add(ship); db.flush()
        shipment_id = ship.shipment_id

        # Home-Consumption GD, mid-workflow: closes at RELEASED.
        gd = GoodsDeclaration(
            shipment_id=shipment_id, lc_id=lc_id, gd_number="KEWB-HC-KGTL",
            gd_type="HOME_CONSUMPTION", status="ASSESSED",
            gross_weight_mt=Decimal("50.000"),
        )
        db.add(gd); db.flush()
        gd_id = gd.gd_id
        db.commit()

        # ---- The closed gate ----
        step("hc_gd_not_closed_at_assessed", not gd_is_closed(gd), {"status": gd.status})
        gd.status = "RELEASED"; db.commit()
        step("hc_gd_closed_at_released", gd_is_closed(gd), {"status": gd.status})

        # An empty GD reports nothing rather than zeros.
        K = kgtl_summary(gd, db)
        step("empty_summary_is_blank_not_zero",
             K["count"] == 0 and K["total_own_kg"] is None and K["total_difference_kg"] is None,
             {"count": K["count"], "total_own_kg": K["total_own_kg"]})

        # ---- Vehicle 1: we received LESS than KGTL released (a shortage) ----
        _add(db, gd, "TLA-123", own=24_850, kgtl=25_000, days_ago=3, own_coils=10, kgtl_coils=10)
        K = kgtl_summary(gd, db)
        e = K["entries"][0]
        step("shortage_row_is_negative",
             e["difference_kg"] == -150.0 and abs(e["difference_pct"] + 0.6) < 1e-9,
             {"difference_kg": e["difference_kg"], "difference_pct": e["difference_pct"]})

        # ---- Vehicle 2: we received MORE than KGTL released ----
        _add(db, gd, "TLB-456", own=25_120, kgtl=25_000, days_ago=2, own_coils=11, kgtl_coils=10)
        K = kgtl_summary(gd, db)
        e = K["entries"][1]
        step("excess_row_is_positive", e["difference_kg"] == 120.0, {"difference_kg": e["difference_kg"]})

        # ---- Totals across both vehicles ----
        K = kgtl_summary(gd, db)
        step("totals_across_vehicles",
             K["count"] == 2 and K["total_own_kg"] == 49_970.0 and K["total_kgtl_kg"] == 50_000.0
             and K["total_difference_kg"] == -30.0 and K["pending_rows"] == 0,
             {"own": K["total_own_kg"], "kgtl": K["total_kgtl_kg"], "diff": K["total_difference_kg"]})

        # Coil counts recorded per vehicle roll up into totals too.
        step("coil_totals_roll_up",
             K["total_own_coils"] == 21 and K["total_kgtl_coils"] == 20,
             {"own_coils": K["total_own_coils"], "kgtl_coils": K["total_kgtl_coils"]})

        # KG -> MT conversion for reporting, alongside the GD's declared weight.
        step("mt_conversion_for_reporting",
             K["total_kgtl_mt"] == 50.0 and abs(K["total_own_mt"] - 49.97) < 1e-9
             and abs(K["total_difference_mt"] + 0.03) < 1e-9
             and K["gd_gross_weight_mt"] == 50.0 and abs(K["kgtl_vs_gd_gross_mt"]) < 1e-9,
             {"own_mt": K["total_own_mt"], "kgtl_mt": K["total_kgtl_mt"],
              "diff_mt": K["total_difference_mt"], "vs_gd": K["kgtl_vs_gd_gross_mt"]})

        # ---- A half-weighed vehicle: the slip for our side has not arrived ----
        pending = _add(db, gd, "TLC-789", own=None, kgtl=25_000, days_ago=1)
        K = kgtl_summary(gd, db)
        e = [x for x in K["entries"] if x["vehicle_number"] == "TLC-789"][0]
        step("partial_row_has_no_difference",
             e["difference_kg"] is None and e["own_weight_kg"] is None
             and e["kgtl_weight_kg"] == 25_000.0,
             {"difference_kg": e["difference_kg"]})

        # ...and it must not drag the total into a fake 25-tonne shortage, nor blank out
        # the difference already established by the two fully-weighed vehicles.
        step("partial_row_excluded_from_total_not_blanking_it",
             K["total_difference_kg"] == -30.0 and K["pending_rows"] == 1 and K["count"] == 3,
             {"total_difference_kg": K["total_difference_kg"], "pending": K["pending_rows"]})

        # Once the missing slip is entered, the total resolves again.
        pending.own_weight_kg = Decimal("24950"); db.commit()
        K = kgtl_summary(gd, db)
        step("total_resolves_when_completed",
             K["pending_rows"] == 0 and K["total_difference_kg"] == -80.0,
             {"total_difference_kg": K["total_difference_kg"]})

        # ---- Sign convention holds at the unit level ----
        step("difference_is_own_minus_kgtl",
             row_difference_kg(Decimal("100"), Decimal("120")) == Decimal("-20")
             and row_difference_kg(None, Decimal("120")) is None
             and row_difference_kg(Decimal("100"), None) is None, None)

        # ---- Rows follow the GD out ----
        db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).delete()
        db.commit()
        left = db.query(GdKgtlWeighment).filter(GdKgtlWeighment.gd_id == gd_id).count()
        step("weighments_cascade_with_gd", left == 0, {"rows_left": left})
        gd_id = None

        return results
    finally:
        db.rollback()
        if gd_id:
            db.query(GdKgtlWeighment).filter(GdKgtlWeighment.gd_id == gd_id).delete()
            db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).delete()
        if shipment_id:
            db.query(Shipment).filter(Shipment.shipment_id == shipment_id).delete()
        if lc_id:
            db.query(LCMaster).filter(LCMaster.lc_id == lc_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    out = run_smoke()
    print(json.dumps(out, indent=2, default=str))
    passed = sum(1 for s in out["steps"] if s["ok"])
    print(f"\n{passed}/{len(out['steps'])} steps passed")
    sys.exit(0 if out.get("ok") else 1)
