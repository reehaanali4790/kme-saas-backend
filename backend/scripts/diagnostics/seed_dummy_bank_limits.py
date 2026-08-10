"""
Seed DUMMY Bank Limits (Pakistani banks) to exercise the Bank Limit Report + its
CSV/Excel/PDF export. Idempotent: re-running first removes prior dummy rows.

Also sets a USD->PKR exchange rate on the 3 existing open test LCs *only if they
currently have none*, so Utilized comes out non-zero. Originals are saved to
_dummy_lc_rate_backup.json so `--undo` can restore them.

Usage:
    python seed_dummy_bank_limits.py          # create dummy limits (+ set LC rates)
    python seed_dummy_bank_limits.py --undo    # remove dummy limits + restore LC rates
"""
import os, sys, json, logging
from datetime import date, datetime

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import SessionLocal
from models.database_models import BankLimit, BankLimitLine, LCMaster, User
from infrastructure.normalization.normalization_service import norm_bank

TAG = "DUMMY_TEST_DATA"
DEMO_RATE = 278.50
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dummy_lc_rate_backup.json")
LC_IMPORTERS = ["MEEN ENTERPRISES (SMC-PVT) LTD", "MAX COMFORT (PVT) LTD",
                "PERFECT CRAFT (SMC-PVT) LTD"]

VF, VT = date(2026, 1, 1), date(2026, 12, 31)

# (bank, branch, group, bank_limit_type, lc_type, group_limit, [ (company, role, lc_type, sub_limit) ])
DUMMY = [
    ("Bank Al Habib", "Korangi, Karachi", "Meen Group", "REGULAR", "BOTH", 300_000_000, [
        ("Meen Group", "PARENT", "BOTH", None),
        ("MEEN ENTERPRISES (SMC-PVT) LTD", "CHILD", "BOTH", 120_000_000),
        ("Horizon Steel (Pvt) Ltd", "CHILD", "SIGHT", 100_000_000),
    ]),
    ("Soneri Bank", "I.I. Chundrigar, Karachi", "Max Group", "REGULAR", "BOTH", 250_000_000, [
        ("Max Group", "PARENT", "BOTH", None),
        ("MAX COMFORT (PVT) LTD", "CHILD", "BOTH", 150_000_000),
        ("Max Logistics (Pvt) Ltd", "CHILD", "DA", 60_000_000),
    ]),
    ("Al Baraka Bank", "Clifton, Karachi", "Perfect Craft Group", "REGULAR", "BOTH", 400_000_000, [
        ("Perfect Craft Group", "PARENT", "BOTH", None),
        ("PERFECT CRAFT (SMC-PVT) LTD", "CHILD", "BOTH", 250_000_000),
        ("Craftline Traders", "CHILD", "SIGHT", 120_000_000),
    ]),
    ("Meezan Bank", "SITE, Karachi", "Sample Traders", "TEMPORARY", "SIGHT", 90_000_000, [
        ("Sample Traders", "PARENT", "SIGHT", None),
        ("Sample Traders Karachi", "CHILD", "SIGHT", 50_000_000),
    ]),
]


def _remove_dummy(db):
    n = db.query(BankLimit).filter(BankLimit.remarks == TAG).count()
    db.query(BankLimit).filter(BankLimit.remarks == TAG).delete(synchronize_session=False)
    db.commit()
    return n


def _restore_rates(db):
    if not os.path.exists(BACKUP):
        print("  (no LC-rate backup found — nothing to restore)")
        return
    orig = json.load(open(BACKUP))
    for row in orig:
        lc = db.query(LCMaster).filter(LCMaster.lc_id == row["lc_id"]).first()
        if lc:
            lc.exchange_rate = row["exchange_rate"]
    db.commit()
    os.remove(BACKUP)
    print(f"  restored exchange_rate on {len(orig)} LC(s)")


def undo():
    db = SessionLocal()
    try:
        print(f"Removed {_remove_dummy(db)} dummy bank limit(s).")
        _restore_rates(db)
    finally:
        db.close()


def seed():
    db = SessionLocal()
    try:
        user = db.query(User).first()
        uid = user.user_id if user else None

        removed = _remove_dummy(db)
        if removed:
            print(f"Cleared {removed} existing dummy limit(s) before reseeding.")

        # give the 3 open test LCs a rate (only if missing) so Utilized is non-zero
        if not os.path.exists(BACKUP):
            backup = []
            for name in LC_IMPORTERS:
                lc = db.query(LCMaster).filter(LCMaster.importer_name == name).first()
                if lc and lc.exchange_rate is None:
                    backup.append({"lc_id": lc.lc_id, "importer": name,
                                   "exchange_rate": None})
                    lc.exchange_rate = DEMO_RATE
            if backup:
                db.commit()
                json.dump(backup, open(BACKUP, "w"), indent=2)
                print(f"Set demo rate {DEMO_RATE} on {len(backup)} LC(s) "
                      f"(originals saved to {os.path.basename(BACKUP)}).")

        created = 0
        for bank, branch, group, blt, lct, glimit, lines in DUMMY:
            bl = BankLimit(
                bank_name=norm_bank(bank), branch=branch, group_company=group,
                bank_limit_type=blt, lc_type=lct, valid_from=VF, valid_to=VT,
                group_limit_amount=glimit, revision_no=1, remarks=TAG,
                created_by=uid, created_at=datetime.utcnow())
            for company, role, clc, sub in lines:
                bl.lines.append(BankLimitLine(
                    company_name=company, limit_type=role, lc_type=clc,
                    sub_limit_amount=sub))
            db.add(bl)
            created += 1
        db.commit()
        print(f"Created {created} dummy bank limit(s): "
              + ", ".join(f"{d[0]}/{d[2]}" for d in DUMMY))
    finally:
        db.close()


if __name__ == "__main__":
    if "--undo" in sys.argv:
        undo()
    else:
        seed()
