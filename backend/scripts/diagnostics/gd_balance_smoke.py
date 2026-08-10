"""
Smoke test the GD Balance Detail report: columns, KG conversion, the duty position
(paid vs potential), and the date / vessel / shipment filters.

  python gd_balance_smoke.py
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import SessionLocal
from models.database_models import (
    ExBondEntry, GoodsDeclaration, LCBuyerAllocation, LCMaster, LCProduct, Shipment,
)
from modules.weboc.helpers.gd_balance_report import gd_balance_report, gd_balance_filter_options
from modules.weboc.helpers.weboc_service import INTO_BOND_DAYS

BONDED_MT = Decimal("500.000")     # 500,000 KG
IB_DUTY = Decimal("10000000")      # PKR 1 Cr assessed on the IB GD


def run_smoke() -> dict:
    db = SessionLocal()
    lc_id = shipment_id = gd_id = None
    results = {"ok": True, "steps": []}

    def step(name, passed, detail=None):
        results["steps"].append({"step": name, "ok": bool(passed), "detail": detail})
        if not passed:
            results["ok"] = False

    def row():
        out = gd_balance_report(db, gd_id=gd_id)
        return out["rows"][0], out["totals"]

    try:
        opened = date.today() - timedelta(days=100)
        lc = LCMaster(
            lc_number=f"GDBAL-SMOKE-{int(datetime.now().timestamp())}",
            lc_date=opened,
            import_date=datetime.combine(opened, datetime.min.time()),
            monitoring_expiry=date.today() + timedelta(days=40),
            status="OPEN",
            importer_name="SMOKE COMFORT (PVT) LTD",
        )
        db.add(lc); db.flush()
        lc_id = lc.lc_id

        db.add(LCProduct(
            lc_id=lc_id, product_code="GPP", item_code="GPP", origin="CHINA",
            quality="PRIME", quantity=Decimal("500"), unit="MT",
            lc_unit_price=Decimal("587"), hs_code="7210.4910", num_containers=0,
        ))
        db.add(LCBuyerAllocation(lc_id=lc_id, buyer_name="JM", share_percent=Decimal("100")))
        db.flush()

        ship = Shipment(lc_id=lc_id, shipment_ref="GDBAL-SMOKE",
                        vessel_name="Smoke Runner", status="PENDING")
        db.add(ship); db.flush()
        shipment_id = ship.shipment_id

        ib_date = date.today() - timedelta(days=100)
        gd = GoodsDeclaration(
            shipment_id=shipment_id, lc_id=lc_id,
            gd_number="KEWB-IB-24-01-07-2026", gd_type="INTO_BOND",
            filing_date=ib_date, custom_office="KEWB",
            gross_weight_mt=BONDED_MT, bonded_qty_mt=BONDED_MT,
            package_count=53, country_of_origin="CHINA",
            # Value/weight gives USD 587/MT — the unit-value column is deliberately left
            # per-KG to prove the report derives the rate rather than reading that column.
            assessed_value_usd=Decimal("293500"), assessed_unit_value_usd=Decimal("0.587"),
            total_duties_pkr=IB_DUTY,
            into_bond_gd_uploaded=True, status="INTO_BOND",
        )
        db.add(gd); db.flush()
        gd_id = gd.gd_id
        db.commit()

        # ---- Sheet columns ----
        r, T = row()
        step("sheet_columns_map",
             r["lc_reference"] == lc.lc_number and r["item"] == "GPP" and r["origin"] == "CHINA"
             and r["importer"] == "SMOKE COMFORT (PVT) LTD" and r["hs_code"] == "7210.4910"
             and r["lc_rate"] == 587.0 and r["custom_collectorate"] == "KEWB"
             and r["packages"] == 53 and r["buyer"] == "JM",
             {k: r[k] for k in ("lc_reference", "item", "origin", "importer", "hs_code",
                                "lc_rate", "custom_collectorate", "packages", "buyer")})

        # GD rate is derived from value/weight, NOT the per-KG unit-value column.
        step("gd_rate_is_per_mt_not_per_kg", r["gd_rate"] == 587.0, {"gd_rate": r["gd_rate"]})

        # 0 containers is a real value, not blank.
        step("zero_containers_is_zero_not_blank", r["containers"] == 0, {"containers": r["containers"]})

        # ---- Weights reported in KG ----
        step("weights_in_kg",
             r["ib_gd_weight_kg"] == 500000.0 and r["balance_ex_bond_kg"] == 500000.0,
             {"ib_gd_weight_kg": r["ib_gd_weight_kg"], "balance": r["balance_ex_bond_kg"]})

        # ---- Expiry uses the SAME rule as the alerts (no second definition) ----
        expected_expiry = ib_date + timedelta(days=INTO_BOND_DAYS)
        step("expiry_matches_bond_rule",
             r["ib_gd_date"] == ib_date.isoformat()
             and r["ib_expiry_date"] == expected_expiry.isoformat()
             and r["remaining_days"] == (expected_expiry - date.today()).days,
             {"ib_gd_date": r["ib_gd_date"], "expiry": r["ib_expiry_date"],
              "remaining_days": r["remaining_days"], "bond_days": INTO_BOND_DAYS})

        # ---- Potential duty: whole lot still in bond -> whole IB duty ----
        step("potential_duty_full_when_nothing_lifted",
             r["potential_duty_pkr"] == 10000000.0 and r["duties_paid_pkr"] is None,
             {"potential": r["potential_duty_pkr"], "paid": r["duties_paid_pkr"]})

        # ---- First Ex-Bond: 231 MT with duty recorded ----
        db.add(ExBondEntry(into_bond_gd_id=gd_id, gd_number="KEWB-EB-388",
                           ex_bond_date=date.today() - timedelta(days=30),
                           quantity_mt=Decimal("231"),
                           duties_paid_pkr=Decimal("4620000"), duty_source="DOCUMENT"))
        db.commit()
        r, T = row()
        step("eb_listed_with_qty_in_kg",
             len(r["eb_entries"]) == 1 and r["eb_entries"][0]["quantity_kg"] == 231000.0
             and r["eb_entries"][0]["gd_number"] == "KEWB-EB-388"
             and r["eb_entries"][0]["duties_paid_pkr"] == 4620000.0,
             r["eb_entries"])

        # Balance = IB weight - lifted, in KG (the sheet's arithmetic).
        step("balance_is_ib_weight_minus_lifted",
             r["balance_ex_bond_kg"] == 269000.0 and r["lifted_kg"] == 231000.0,
             {"balance": r["balance_ex_bond_kg"], "lifted": r["lifted_kg"]})

        # Paid = what's recorded; potential = IB duty apportioned to the balance.
        step("duty_position_after_first_eb",
             r["duties_paid_pkr"] == 4620000.0
             and abs(r["potential_duty_pkr"] - 10000000.0 * (269.0 / 500.0)) < 0.01,
             {"paid": r["duties_paid_pkr"], "potential": r["potential_duty_pkr"]})

        # ---- Second Ex-Bond with NO duty recorded ----
        db.add(ExBondEntry(into_bond_gd_id=gd_id, gd_number="KEWB-EB-401",
                           ex_bond_date=date.today() - timedelta(days=10),
                           quantity_mt=Decimal("100")))
        db.commit()
        r, T = row()
        step("missing_duty_is_flagged_not_silently_zero",
             r["duties_paid_missing_count"] == 1 and r["duties_paid_pkr"] == 4620000.0
             and T["rows_missing_paid_duty"] == 1,
             {"missing": r["duties_paid_missing_count"], "paid": r["duties_paid_pkr"],
              "totals_flag": T["rows_missing_paid_duty"]})

        # ---- Settling the bond drops the potential duty to zero ----
        db.add(ExBondEntry(into_bond_gd_id=gd_id, gd_number="KEWB-EB-410",
                           ex_bond_date=date.today() - timedelta(days=2),
                           quantity_mt=Decimal("164"),
                           duties_paid_pkr=Decimal("3280000"), duty_source="MANUAL"))
        db.commit()
        r, T = row()
        # 231 + 100 + 164 = 495 of 500 -> 5 MT (1%) left, inside the settle tolerance.
        step("settled_bond_owes_nothing_more",
             r["is_settled"] and r["potential_duty_pkr"] == 0.0
             and r["duties_paid_pkr"] == 7900000.0,
             {"settled": r["is_settled"], "potential": r["potential_duty_pkr"],
              "paid": r["duties_paid_pkr"], "state": r["bond_state"]})

        # ---- Totals ----
        out = gd_balance_report(db, gd_id=gd_id)
        T = out["totals"]
        step("totals_roll_up",
             T["gd_count"] == 1 and T["settled_count"] == 1 and T["open_count"] == 0
             and T["duties_paid_pkr"] == 7900000.0 and T["potential_duty_pkr"] == 0.0
             and T["ib_gd_weight_kg"] == 500000.0,
             T)

        # A settled GD holds no balance, so it must not inflate "balance in bond".
        step("settled_gd_excluded_from_balance_total",
             T["balance_ex_bond_kg"] == 0.0, {"balance_total": T["balance_ex_bond_kg"]})

        # ---- Filters ----
        step("filter_open_only_hides_settled",
             len(gd_balance_report(db, gd_id=gd_id, include_settled=False)["rows"]) == 0, None)
        step("filter_by_vessel_matches",
             len(gd_balance_report(db, gd_id=gd_id, vessel="smoke runner")["rows"]) == 1
             and len(gd_balance_report(db, gd_id=gd_id, vessel="NO SUCH VESSEL")["rows"]) == 0, None)
        step("filter_by_shipment_matches",
             len(gd_balance_report(db, shipment_id=shipment_id)["rows"]) == 1
             and len(gd_balance_report(db, shipment_id=999_999_999)["rows"]) == 0, None)

        # Date filter runs on the IB GD date shown in the row.
        step("filter_by_date_range",
             len(gd_balance_report(db, gd_id=gd_id, date_from=ib_date,
                                   date_to=ib_date)["rows"]) == 1
             and len(gd_balance_report(db, gd_id=gd_id,
                                       date_from=ib_date + timedelta(days=1))["rows"]) == 0
             and len(gd_balance_report(db, gd_id=gd_id,
                                       date_to=ib_date - timedelta(days=1))["rows"]) == 0, None)

        # ---- Filter options only offer values that can return rows ----
        opts = gd_balance_filter_options(db)
        step("filter_options_include_this_gd",
             "Smoke Runner" in opts["vessels"]
             and any(s["shipment_id"] == shipment_id for s in opts["shipments"]), None)

        # ---- Non-IB GDs stay out of a bond-balance report ----
        gd.gd_type = "HOME_CONSUMPTION"
        db.commit()
        step("home_consumption_gd_excluded",
             len(gd_balance_report(db, gd_id=gd_id)["rows"]) == 0, None)

        return results
    finally:
        db.rollback()
        if gd_id:
            db.query(ExBondEntry).filter(ExBondEntry.into_bond_gd_id == gd_id).delete()
            db.query(GoodsDeclaration).filter(GoodsDeclaration.gd_id == gd_id).delete()
        if shipment_id:
            db.query(Shipment).filter(Shipment.shipment_id == shipment_id).delete()
        if lc_id:
            db.query(LCBuyerAllocation).filter(LCBuyerAllocation.lc_id == lc_id).delete()
            db.query(LCProduct).filter(LCProduct.lc_id == lc_id).delete()
            db.query(LCMaster).filter(LCMaster.lc_id == lc_id).delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    out = run_smoke()
    print(json.dumps(out, indent=2, default=str))
    passed = sum(1 for s in out["steps"] if s["ok"])
    print(f"\n{passed}/{len(out['steps'])} steps passed")
    sys.exit(0 if out.get("ok") else 1)
