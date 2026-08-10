"""
Into-bond 180-day clock alerts.

Rules (the clock itself lives in weboc_service.bond_summary — this module only decides
who gets told and how often):

  1. DUE_SOON      day 160 of 180 (20 days left) -> daily re-alert until settled.
                   Acknowledging quiets it for BOND_ALERT_ACK_HOURS, then it returns
                   daily while the bond is still open. Same cadence as the FI alerts.
  2. PASSED        deadline blown and cargo still in bond -> daily re-alert, penalty
                   accruing. The user records the amount off the document or by hand.
  3. CLEARED_LATE  bond settled, but the settling Ex-Bond is dated after the deadline
                   -> the bond is closed, yet a penalty is owed. Chase the amount daily
                   until it is recorded, then go quiet.
  4. CLEARED       settled inside the window -> resolve EVERYTHING for this GD.

(4) is what makes a late-entered Ex-Bond safe. If the user forgets to feed the Ex-Bond,
the system will happily alert that 180 days have passed and a penalty is due. When the
settling Ex-Bond is finally uploaded and its date proves the bond closed inside the
window, those alerts are resolved and any penalty already recorded is voided — the
breach never actually happened, so nothing about it survives.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from config.settings import settings
from models.database_models import GoodsDeclaration, Shipment
from infrastructure.alerts.recurring_alerts import (
    create_once,
    create_recurring_alert,
    family_key,
    resolve_family,
)
from modules.weboc.helpers.weboc_service import INTO_BOND_DAYS, BOND_WARN_LEAD, bond_summary

logger = logging.getLogger("uvicorn")

ALERT_BOND_DUE = "INTO_BOND_DUE"
ALERT_BOND_OVERDUE = "INTO_BOND_OVERDUE"
ALERT_BOND_PENALTY_DUE = "INTO_BOND_PENALTY_DUE"
ALERT_BOND_PENALTY_VOID = "INTO_BOND_PENALTY_VOID"


def _ref(gd: GoodsDeclaration) -> str:
    if gd.gd_number:
        return gd.gd_number
    if gd.shipment and gd.shipment.shipment_ref:
        return gd.shipment.shipment_ref
    return f"GD-{gd.gd_id}"


def _load_ib_gds(db: Session, gd_ids: Optional[Sequence[int]] = None) -> list[GoodsDeclaration]:
    q = (
        db.query(GoodsDeclaration)
        .options(joinedload(GoodsDeclaration.shipment).joinedload(Shipment.lc))
        .filter(GoodsDeclaration.gd_type == "INTO_BOND")
    )
    if gd_ids:
        q = q.filter(GoodsDeclaration.gd_id.in_(list(gd_ids)))
    return q.all()


def void_stale_penalty(db: Session, gd: GoodsDeclaration, bond: dict) -> bool:
    """Drop a penalty recorded against a breach that a back-dated Ex-Bond disproved.

    Kept as an explicit step rather than a cascade: the amount is cleared so no report
    counts it, and the reason is rewritten to say why, so the audit trail explains the
    reversal instead of the row just vanishing.
    """
    if not bond.get("penalty_stale"):
        return False
    was = bond.get("penalty_pkr")
    gd.bond_penalty_pkr = None
    gd.bond_penalty_source = None
    gd.bond_penalty_days = None
    gd.bond_penalty_reason = (
        f"Voided automatically on {date.today().isoformat()}: Ex-Bond dated "
        f"{bond.get('settlement_date')} settled the bond by the {bond.get('deadline')} "
        f"deadline, so the {INTO_BOND_DAYS}-day window was never breached. "
        f"Penalty previously recorded: PKR {was:,.0f}."
        if was is not None else
        f"Voided automatically on {date.today().isoformat()}: bond settled within the "
        f"{INTO_BOND_DAYS}-day window."
    )
    return True


def scan_bond_alerts(
    db: Session,
    gd_ids: Optional[Sequence[int]] = None,
    *,
    cooldown_hours: Optional[int] = None,
    today: Optional[date] = None,
) -> dict:
    cooldown = cooldown_hours if cooldown_hours is not None else settings.BOND_ALERT_ACK_HOURS
    today = today or date.today()
    created = 0
    resolved = 0
    voided = 0

    for gd in _load_ib_gds(db, gd_ids):
        bond = bond_summary(gd, db, today)
        if not bond.get("applies"):
            continue

        fam_due = family_key(ALERT_BOND_DUE, gd.gd_id)
        fam_overdue = family_key(ALERT_BOND_OVERDUE, gd.gd_id)
        fam_penalty = family_key(ALERT_BOND_PENALTY_DUE, gd.gd_id)
        state = bond["state"]
        ref = _ref(gd)
        lc_id = gd.lc_id
        ship_id = gd.shipment_id
        common = dict(entity_type="GD", entity_id=gd.gd_id, lc_id=lc_id, shipment_id=ship_id)

        # --- Settled inside the window: nothing is owed, nothing stays open. ---
        if state == "CLEARED":
            resolved += resolve_family(db, fam_due)
            resolved += resolve_family(db, fam_overdue)
            resolved += resolve_family(db, fam_penalty)
            if void_stale_penalty(db, gd, bond):
                voided += 1
                # One-shot receipt so the reversal is visible, not silent.
                if create_once(
                    db,
                    alert_type=ALERT_BOND_PENALTY_VOID,
                    dedup_key=f"{ALERT_BOND_PENALTY_VOID}:{gd.gd_id}:{bond['settlement_date']}",
                    severity="LOW",
                    title=f"Into-bond penalty voided — {ref}",
                    message=(
                        f"The Ex-Bond dated {bond['settlement_date']} settled this bond on or "
                        f"before its {bond['deadline']} deadline, so the {INTO_BOND_DAYS}-day "
                        f"window was never breached. The penalty recorded against it has been "
                        f"voided and is excluded from reporting."
                    ),
                    **common,
                ):
                    created += 1
            continue

        # --- Settled, but the settling lifting was late: bond closed, penalty owed. ---
        if state == "CLEARED_LATE":
            resolved += resolve_family(db, fam_due)
            resolved += resolve_family(db, fam_overdue)
            if bond["penalty_recorded"]:
                resolved += resolve_family(db, fam_penalty)
                continue
            late_by = bond["settled_late_by_days"]
            if create_recurring_alert(
                db,
                alert_type=ALERT_BOND_PENALTY_DUE,
                family_prefix=fam_penalty,
                severity="HIGH",
                title=f"Record the into-bond penalty — {ref} settled {late_by} day(s) late",
                message=(
                    f"This bond was settled by an Ex-Bond dated {bond['settlement_date']}, "
                    f"which is {late_by} day(s) past the {bond['deadline']} deadline "
                    f"(IB {bond['ib_start_date']} + {INTO_BOND_DAYS} days). A penalty is "
                    f"payable. Record the amount from the Ex-Bond / demand notice, or enter "
                    f"it manually with the reason. This alert repeats daily until it is."
                ),
                cooldown_hours=cooldown,
                **common,
            ):
                created += 1
            continue

        # --- Deadline blown and cargo still in bond: penalty accruing. ---
        if state == "PASSED":
            resolved += resolve_family(db, fam_due)
            rem = bond["remaining_qty_mt"] or 0
            over = bond["overdue_days"]
            pen = bond["penalty_pkr"]
            pen_txt = (
                f" Penalty recorded so far: PKR {pen:,.0f}." if pen is not None
                else " No penalty amount has been recorded yet — add it from the demand "
                     "notice, or manually with the reason."
            )
            if create_recurring_alert(
                db,
                alert_type=ALERT_BOND_OVERDUE,
                family_prefix=fam_overdue,
                severity="HIGH",
                title=f"Into-bond {INTO_BOND_DAYS}-day deadline PASSED — {ref}",
                message=(
                    f"{rem:,.3f} MT of {bond['bonded_qty_mt']:,.3f} MT is still in bond. The "
                    f"{INTO_BOND_DAYS}-day deadline ({bond['deadline']} = IB "
                    f"{bond['ib_start_date']} + {INTO_BOND_DAYS} days) passed {over} day(s) "
                    f"ago and a penalty is accruing. Lift the remaining quantity via an "
                    f"Ex-Bond GD.{pen_txt} If it was already lifted, upload the Ex-Bond — if "
                    f"its date is inside the window this alert and any penalty are dropped."
                ),
                cooldown_hours=cooldown,
                **common,
            ):
                created += 1
            continue

        # --- Day 160 of 180: start the daily reminder. ---
        if state == "DUE_SOON":
            resolved += resolve_family(db, fam_overdue)
            resolved += resolve_family(db, fam_penalty)
            days = bond["days_remaining"]
            rem = bond["remaining_qty_mt"] or 0
            if create_recurring_alert(
                db,
                alert_type=ALERT_BOND_DUE,
                family_prefix=fam_due,
                severity="HIGH" if days <= 10 else "MEDIUM",
                title=f"Into-bond deadline in {days} day(s) — {ref}",
                message=(
                    f"{rem:,.3f} MT of {bond['bonded_qty_mt']:,.3f} MT is still in bond and "
                    f"must be lifted by {bond['deadline']} (IB {bond['ib_start_date']} + "
                    f"{INTO_BOND_DAYS} days) — {days} day(s) away. Past that a penalty "
                    f"applies. This alert repeats daily until the bond is settled."
                ),
                cooldown_hours=cooldown,
                **common,
            ):
                created += 1
            continue

        # --- OK / NO_ETA: more than the lead window away, or no clock to run. ---
        resolved += resolve_family(db, fam_due)
        resolved += resolve_family(db, fam_overdue)
        resolved += resolve_family(db, fam_penalty)
        if void_stale_penalty(db, gd, bond):
            voided += 1

    summary = {"created": created, "resolved": resolved, "penalties_voided": voided}
    logger.info("[Bond 180-day alerts] %s", summary)
    return summary


def run_bond_alert_scan(db: Session, gd_ids: Optional[Sequence[int]] = None) -> dict:
    result = scan_bond_alerts(db, gd_ids)
    db.commit()
    return result
