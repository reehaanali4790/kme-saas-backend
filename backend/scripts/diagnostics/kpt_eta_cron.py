"""
KPT port sync — manual runner / smoke test.

Examples:
  python kpt_eta_cron.py --smoke
  python kpt_eta_cron.py --smoke --vessel "LISA GLORY"
  python kpt_eta_cron.py --smoke-on-port
  python kpt_eta_cron.py --smoke-on-port --vessel "Yu Tong"
  python kpt_eta_cron.py --smoke-departures
  python kpt_eta_cron.py --run-port-cycle
  python kpt_eta_cron.py --run-all
  python kpt_eta_cron.py --e2e-test
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import SessionLocal
from config.settings import settings
from models.database_models import LCMaster, Shipment
from integrations.kpt.kpt_eta_sync import run_kpt_eta_sync, smoke_test_crawler
from integrations.kpt.kpt_on_port_sync import ON_PORT_STATUS, run_kpt_on_port_sync, smoke_test_on_port_crawler
from integrations.kpt.kpt_departures_sync import run_kpt_departures_sync, smoke_test_departures_crawler


def run_e2e_test(vessel_name: str = "Yu Tong") -> dict:
    """Create temp LC+shipment, run ETA then on-port sync, verify, cleanup."""
    db = SessionLocal()
    lc_id = None
    shipment_id = None
    try:
        old_lc = date.today() - timedelta(days=30)
        lc = LCMaster(
            lc_number=f"KPT-TEST-{int(datetime.now().timestamp())}",
            lc_date=old_lc,
            import_date=datetime.combine(old_lc, datetime.min.time()),
            monitoring_expiry=date.today() + timedelta(days=40),
            status="OPEN",
        )
        db.add(lc)
        db.flush()
        lc_id = lc.lc_id

        ship = Shipment(
            lc_id=lc_id,
            shipment_ref="KPT-E2E-TEST",
            vessel_name=vessel_name,
            status="PENDING",
        )
        db.add(ship)
        db.commit()
        shipment_id = ship.shipment_id

        eta_res = run_kpt_eta_sync(
            db,
            min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
            url=settings.KPT_ETA_URL,
        )
        db.refresh(ship)
        after_eta = {"eta": ship.eta.isoformat() if ship.eta else None, "vessel_location": ship.vessel_location}

        on_port_res = run_kpt_on_port_sync(
            db,
            min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
            url=settings.KPT_ON_PORT_URL,
        )
        db.refresh(ship)
        after_on_port = {
            "eta": ship.eta.isoformat() if ship.eta else None,
            "vessel_location": ship.vessel_location,
        }

        ok = ship.vessel_location == ON_PORT_STATUS
        result = {
            "ok": ok,
            "vessel": vessel_name,
            "lc_id": lc_id,
            "shipment_id": shipment_id,
            "eta_sync": {
                "matched": eta_res["matched"],
                "updated_shipments": eta_res["updated_shipments"],
                "after": after_eta,
            },
            "on_port_sync": {
                "matched": on_port_res["matched"],
                "updated_shipments": on_port_res["updated_shipments"],
                "after": after_on_port,
            },
            "expected_port_status": ON_PORT_STATUS,
        }
        return result
    finally:
        if shipment_id:
            db.query(Shipment).filter(Shipment.shipment_id == shipment_id).delete()
        if lc_id:
            db.query(LCMaster).filter(LCMaster.lc_id == lc_id).delete()
        db.commit()
        db.close()


def main():
    parser = argparse.ArgumentParser(description="KPT Expected Arrivals + Ships On Port sync")
    parser.add_argument("--smoke", action="store_true", help="Crawl KPT expected arrivals")
    parser.add_argument("--smoke-on-port", action="store_true", help="Crawl KPT ships on port")
    parser.add_argument("--smoke-departures", action="store_true", help="Crawl KPT ship departures")
    parser.add_argument("--run", action="store_true", help="Run ETA sync for eligible vessels")
    parser.add_argument("--run-on-port", action="store_true", help="Run on-port sync for eligible vessels")
    parser.add_argument("--run-departures", action="store_true", help="Run departures sync for eligible vessels")
    parser.add_argument("--run-port-cycle", action="store_true", help="Run on-port then departures sync")
    parser.add_argument("--run-all", action="store_true", help="Run ETA, on-port, then departures sync")
    parser.add_argument("--e2e-test", action="store_true", help="Temp test row, sync both, cleanup")
    parser.add_argument("--dry-run", action="store_true", help="With --run*: do not write to DB")
    parser.add_argument("--vessel", type=str, help="Vessel name to look up during --smoke*")
    args = parser.parse_args()

    if not any([
        args.smoke, args.smoke_on_port, args.smoke_departures,
        args.run, args.run_on_port, args.run_departures, args.run_port_cycle, args.run_all, args.e2e_test,
    ]):
        parser.error(
            "pass --smoke, --smoke-on-port, --smoke-departures, --run, --run-on-port, "
            "--run-departures, --run-port-cycle, --run-all, and/or --e2e-test"
        )

    if args.smoke:
        result = smoke_test_crawler(args.vessel, url=settings.KPT_ETA_URL)
        print(json.dumps(result, indent=2))

    if args.smoke_on_port:
        result = smoke_test_on_port_crawler(args.vessel, url=settings.KPT_ON_PORT_URL)
        print(json.dumps(result, indent=2))

    if args.smoke_departures:
        result = smoke_test_departures_crawler(args.vessel, url=settings.KPT_DEPARTURES_URL)
        print(json.dumps(result, indent=2))

    if args.e2e_test:
        vessel = args.vessel or "Yu Tong"
        result = run_e2e_test(vessel)
        print(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            sys.exit(1)

    db = None
    if args.run or args.run_on_port or args.run_departures or args.run_port_cycle or args.run_all:
        db = SessionLocal()

    try:
        if args.run or args.run_all:
            result = run_kpt_eta_sync(
                db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_ETA_URL,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, indent=2, default=str))

        if args.run_on_port or args.run_port_cycle or args.run_all:
            result = run_kpt_on_port_sync(
                db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_ON_PORT_URL,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, indent=2, default=str))

        if args.run_departures or args.run_port_cycle or args.run_all:
            result = run_kpt_departures_sync(
                db,
                min_lc_age_days=settings.KPT_ETA_LC_AGE_DAYS,
                url=settings.KPT_DEPARTURES_URL,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, indent=2, default=str))
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    main()
