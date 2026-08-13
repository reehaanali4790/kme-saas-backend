"""
WeBOC rules — GD filing deadline, into-bond / ex-bond quantity tracking and the 180-day
bond deadline, plus the cross-checks run on the GD View and Item Details documents.

Single source of truth for these rules: the WeBOC tab, the SRO report and the alert engine
all call in here, so a rule change lands everywhere at once.

Deadlines:
  * GD filing deadline  = ETA + 18 days   (remind from 10 days before it)
  * Into-bond settlement = first IB GD date + 180 days
      (filing_date, else date parsed from GD number, else ETA)
      (remind from 20 days before it — i.e. from day 160 of 180)
  * IB weight settlement = cumulative Ex-Bond (XB) liftings vs Gross Weight,
      within a 1–2% tolerance band. GD is not complete until settled.
  * 180-day breach = a penalty is payable. Breach is judged on the date of the Ex-Bond
      that SETTLES the weight, not on when it was entered: an Ex-Bond keyed in late but
      dated inside the window clears the bond and voids any penalty raised meanwhile.
      The penalty amount is read off the document or entered by the user — never computed.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.exceptions import ValidationError
from models.database_models import (
    GoodsDeclaration, ExBondEntry, Shipment, LCMaster, BillOfLading,
    CommercialInvoice, PackingList,
)

logger = logging.getLogger("uvicorn")

GD_FILING_DAYS = 18          # GD must be filed within 18 days of ETA
GD_FILING_ALERT_LEAD = 10    # start reminding 10 days before that deadline
INTO_BOND_DAYS = 180         # bonded material must be lifted within 180 days of first IB
BOND_WARN_LEAD = 20          # start reminding at day 160 of 180 (20 days remaining)
WEIGHT_TOLERANCE_MT = 0.5    # same tolerance the shipment validator uses
# IB settlement: remaining <= this % of gross bonded qty counts as settled (user range 1–2%).
BOND_SETTLE_TOLERANCE_PCT = Decimal("0.02")
BOND_SETTLE_TOLERANCE_MIN_PCT = Decimal("0.01")


def _f(v):
    return float(v) if v is not None else None


def _d(v):
    return Decimal(str(v)) if v is not None else Decimal("0")


def _d_pos(v):
    """Positive Decimal or None (does not coerce missing to 0)."""
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d > 0 else None


from infrastructure.normalization.smart_match import names_equivalent


def _norm(s):
    s = re.sub(r"[^A-Za-z0-9]+", " ", str(s or "")).strip().upper()
    return re.sub(r"\s+", " ", s)


def _lenient_match(a, b, kind: str = "generic"):
    """Case/punctuation-insensitive smart match. None when either side is empty."""
    return names_equivalent(a, b, kind=kind)  # type: ignore[arg-type]


def _norm_hs(code):
    digits = re.sub(r"\D", "", str(code or ""))
    return digits or None


# ---------------------------------------------------------------------------
# Declaration type
# ---------------------------------------------------------------------------
def classify_declaration(raw) -> str:
    """Map a printed declaration type to the internal GD type.
    Returns HOME_CONSUMPTION / INTO_BOND / EX_BOND / UNKNOWN. Ex-bond is checked BEFORE
    into-bond: 'Ex Bond' contains the word 'bond' and must not be read as into-bond."""
    t = _norm(raw)
    if not t:
        return "UNKNOWN"
    if re.search(r"\bEX\b|EX BOND|EXBOND|EX WAREHOUS|\bXB\b", t):
        return "EX_BOND"
    if re.search(
        r"\bIB\b|INTO BOND|INBOND|IN BOND|INBOUND|IN BOND PUBLIC|WAREHOUS",
        t,
    ):
        return "INTO_BOND"
    if re.search(r"\bHC\b|HOME CONSUMPTION", t):
        return "HOME_CONSUMPTION"
    return "UNKNOWN"


def classify_from_gd_number(gd_number) -> str:
    """KEWB-IB-234-03-07-2026 / KEWB-HC-16311-... → internal type, else UNKNOWN."""
    u = str(gd_number or "").upper()
    if not u:
        return "UNKNOWN"
    if re.search(r"(^|[\-/])IB([\-/]|$)", u):
        return "INTO_BOND"
    if re.search(r"(^|[\-/])HC([\-/]|$)", u):
        return "HOME_CONSUMPTION"
    if re.search(r"(^|[\-/])(XB|EX)([\-/]|$)", u):
        return "EX_BOND"
    return "UNKNOWN"


def resolve_gd_type_with_eb_guard(gd: GoodsDeclaration, candidate_type: str | None) -> str | None:
    """Ex-Bond can never exist as a shipment's own top-level GD — it is always a Partial
    GD (EB Release) recorded against an existing Into-Bond GD. Called whenever a GD View
    (auto-extracted or manually saved) or a manual declaration-type override would set
    this GD's gd_type to EX_BOND:
      * If this shipment already has an Into-Bond context (gd_type already INTO_BOND, or
        the Into-Bond GD has been uploaded), an EB-looking GD View does not overwrite the
        shipment's own classification — the user should use the Partial GD workflow, so
        the existing type is left untouched and nothing is blocked.
      * Otherwise (no IB exists yet for this shipment) this is a direct/standalone EB
        upload, which is invalid — raise so the caller rejects it with a clear message.
    Returns the gd_type that should actually be stored (None/other values pass through
    unchanged)."""
    if candidate_type != "EX_BOND":
        return candidate_type
    if (gd.gd_type or "") == "INTO_BOND" or bool(gd.into_bond_gd_uploaded):
        return gd.gd_type
    raise ValidationError(
        "This document looks like an Ex-Bond (EB) declaration, but no Into-Bond GD "
        "exists yet for this shipment. An Ex-Bond release can only be recorded against "
        "an existing Into-Bond GD — file the Into-Bond GD first, then record this as a "
        "Partial GD."
    )


def parse_dd_mm_yyyy(value) -> date | None:
    """Parse a Pakistan-format date DD-MM-YYYY or DD/MM/YYYY (day first)."""
    s = str(value or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*$", s)
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def parse_date_from_gd_number(gd_number) -> date | None:
    """Parse trailing DD-MM-YYYY / DD/MM/YYYY from a WeBOC GD number."""
    return parse_dd_mm_yyyy(str(gd_number or "").strip())


def resolve_field58_filing_date(data: dict) -> date | None:
    """Authoritative filing date from Field 58 Machine No. & Date (DD-MM-YYYY).

    WeBOC GD numbers embed the Field 58 date as the last DD-MM-YYYY segment
    (e.g. KEWB-IB-234-03-07-2026 -> 3 Jul 2026). That suffix wins over AI filing_date
    so US-style MM-DD misreads and filename dates are ignored.
    """
    if not data:
        return None

    d = parse_date_from_gd_number(data.get("gd_number"))
    if d:
        return d

    for key in ("field58_date", "machine_date"):
        d = parse_dd_mm_yyyy(data.get(key))
        if d:
            return d

    for key in ("machine_number", "machine_no_and_date", "field58_machine_and_date"):
        text = str(data.get(key) or "").strip()
        if not text:
            continue
        d = parse_date_from_gd_number(text)
        if d:
            return d
        m = re.search(r"(?<![\d\-/])(\d{2})[-/](\d{2})[-/](\d{4})(?![\d])", text)
        if m:
            dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return date(yyyy, mm, dd)
            except ValueError:
                pass

    return parse_dd_mm_yyyy(data.get("filing_date"))


def normalize_gd_filing_date(data: dict) -> dict:
    """Set filing_date from Field 58 when it can be resolved (mutates data)."""
    if not data:
        return data
    resolved = resolve_field58_filing_date(data)
    if resolved:
        data["filing_date"] = resolved.isoformat()
    return data


def resolve_bonded_qty(gd: GoodsDeclaration) -> Decimal | None:
    """Gross-first bonded target for IB settlement."""
    for v in (
        _d_pos(gd.bonded_qty_mt),
        _d_pos(gd.gross_weight_mt),
        _d_pos(gd.net_weight_mt),
    ):
        if v is not None:
            return v
    return None


def settle_tolerance_mt(bonded: Decimal) -> Decimal:
    if bonded <= 0:
        return Decimal("0")
    return max(bonded * BOND_SETTLE_TOLERANCE_PCT, bonded * BOND_SETTLE_TOLERANCE_MIN_PCT)


def is_weight_settled(bonded: Decimal | None, remaining: Decimal | None) -> bool:
    """True when remaining (or slight over-lift) is within the 1–2% tolerance of gross."""
    if bonded is None or bonded <= 0 or remaining is None:
        return False
    tol = settle_tolerance_mt(bonded)
    if remaining <= tol:
        return True
    # slight over-lift within tolerance also counts as settled
    if remaining < 0 and abs(remaining) <= tol:
        return True
    return False


def settlement_date_from_entries(entries, bonded: Decimal | None) -> date | None:
    """Ex-Bond date that completed weight settlement (last lifting that closes the bond)."""
    if not entries or bonded is None or bonded <= 0:
        return None
    cumulative = Decimal("0")
    for e in entries:
        cumulative += _d(e.quantity_mt) or Decimal("0")
        if is_weight_settled(bonded, bonded - cumulative):
            return e.ex_bond_date
    dated = [e.ex_bond_date for e in entries if e.ex_bond_date]
    return max(dated) if dated else None


def ib_anchor_date(gd: GoodsDeclaration) -> date | None:
    """Start of the 180-day settle window: first IB GD date."""
    if gd.filing_date:
        return gd.filing_date
    parsed = parse_date_from_gd_number(gd.gd_number)
    if parsed:
        return parsed
    if gd.eta:
        return gd.eta
    return None


# ---------------------------------------------------------------------------
# GD filing deadline (ETA + 18 days)
# ---------------------------------------------------------------------------
def gd_is_filed(gd: GoodsDeclaration) -> bool:
    """The GD counts as filed once the GD View is in (or the GD otherwise carries a number)."""
    return bool(gd.gd_view_uploaded or gd.gd_number or gd.filing_date or gd.final_gd_uploaded)


def filing_deadline(gd: GoodsDeclaration, today: date = None) -> dict:
    """GD filing deadline from the ETA. state:
       NO_ETA / OK / DUE_SOON (within the 10-day lead) / OVERDUE / FILED / LATE_FILED."""
    today = today or date.today()
    eta = gd.eta
    filed = gd_is_filed(gd)
    late = bool(gd.late_filed) or (gd.section82_penalty_pkr or 0) > 0
    if not eta:
        return {"eta": None, "deadline": None, "days_remaining": None,
                "state": "LATE_FILED" if late else ("FILED" if filed else "NO_ETA"),
                "filed": filed, "late_filed": late}

    deadline = eta + timedelta(days=GD_FILING_DAYS)
    days = (deadline - today).days
    if late:
        state = "LATE_FILED"
    elif filed:
        state = "FILED"
    elif days < 0:
        state = "OVERDUE"
    elif days <= GD_FILING_ALERT_LEAD:
        state = "DUE_SOON"
    else:
        state = "OK"
    return {"eta": eta.isoformat(), "deadline": deadline.isoformat(),
            "days_remaining": days, "state": state, "filed": filed, "late_filed": late,
            "filing_days": GD_FILING_DAYS, "alert_lead_days": GD_FILING_ALERT_LEAD}


# ---------------------------------------------------------------------------
# Into-bond / ex-bond
# ---------------------------------------------------------------------------
def bond_summary(gd: GoodsDeclaration, db: Session, today: date = None) -> dict:
    """Into-bond gross-weight settlement vs Ex-Bond liftings, plus the 180-day clock
    from the first IB GD date. Returns applies=False for non-IB GDs."""
    today = today or date.today()
    if (gd.gd_type or "") != "INTO_BOND":
        return {"applies": False}

    entries = (db.query(ExBondEntry)
                 .filter(ExBondEntry.into_bond_gd_id == gd.gd_id)
                 .order_by(ExBondEntry.ex_bond_date, ExBondEntry.entry_id).all())
    bonded = resolve_bonded_qty(gd)
    bonded_d = bonded if bonded is not None else Decimal("0")
    lifted = sum((_d(e.quantity_mt) for e in entries), Decimal("0"))
    raw_remaining = (bonded_d - lifted) if bonded is not None else None
    tol = settle_tolerance_mt(bonded_d) if bonded is not None else Decimal("0")
    settled = is_weight_settled(bonded, raw_remaining) if bonded is not None else False
    remaining = max(Decimal("0"), raw_remaining) if (raw_remaining is not None and (settled or raw_remaining < Decimal("0"))) else raw_remaining
    over_amt = (lifted - bonded_d) if bonded is not None and lifted > bonded_d else Decimal("0")
    # Hard over-lift only when beyond settle tolerance
    over = bool(bonded is not None and over_amt > tol)

    start = ib_anchor_date(gd)
    deadline = (start + timedelta(days=INTO_BOND_DAYS)) if start else None
    settled_on = settlement_date_from_entries(entries, bonded) if settled else None
    # Timer runs IB start -> settling EB date when complete; otherwise IB start -> today.
    timer_end = settled_on if settled and settled_on else today
    days = (deadline - today).days if deadline else None
    days_elapsed = max(0, (timer_end - start).days) if start else None
    timer_pct = None
    if days_elapsed is not None:
        timer_pct = round(min(100.0, max(0.0, (days_elapsed / INTO_BOND_DAYS) * 100)), 1)

    fulfilled_pct = None
    if bonded is not None and bonded_d > 0:
        fulfilled_pct = float(min(Decimal("100"), (lifted / bonded_d) * Decimal("100")))

    att_ids = [e.attachment_id for e in entries if e.attachment_id]
    att_map = {}
    if att_ids:
        from models.database_models import GDAttachment
        for att in db.query(GDAttachment).filter(GDAttachment.attachment_id.in_(att_ids)).all():
            att_map[att.attachment_id] = att

    # Partial GD (business term) / EB Release — its own EB GD View + Item Details
    # attachments, and the quota approval it was validated against, keyed by entry_id so
    # the history list below can show each release's full document trail in one query.
    entry_ids = [e.entry_id for e in entries]
    partial_atts_by_entry: dict[int, list] = {eid: [] for eid in entry_ids}
    partial_item_counts: dict[int, int] = {eid: 0 for eid in entry_ids}
    approval_labels: dict[int, str] = {}
    if entry_ids:
        from models.database_models import GDAttachment, ExBondItem, EdbApproval

        for att in (db.query(GDAttachment)
                      .filter(GDAttachment.ex_bond_entry_id.in_(entry_ids)).all()):
            partial_atts_by_entry.setdefault(att.ex_bond_entry_id, []).append({
                "attachment_id": att.attachment_id, "kind": att.kind, "filename": att.filename,
            })
        for eid, cnt in (db.query(ExBondItem.entry_id, func.count(ExBondItem.item_id))
                           .filter(ExBondItem.entry_id.in_(entry_ids))
                           .group_by(ExBondItem.entry_id).all()):
            partial_item_counts[eid] = cnt
        approval_ids = {e.approval_id for e in entries if e.approval_id}
        if approval_ids:
            for a in db.query(EdbApproval).filter(EdbApproval.approval_id.in_(approval_ids)).all():
                approval_labels[a.approval_id] = a.main_sro_no or a.approval_no or str(a.approval_id)

    # Was the bond settled late? Judged on the SETTLING EX-BOND DATE, never on when the
    # user got round to entering it. A back-dated Ex-Bond proving settlement inside the
    # window clears the bond outright, even if it is keyed in months after the deadline.
    settled_late = bool(settled and settled_on and deadline and settled_on > deadline)
    settled_late_by = (settled_on - deadline).days if settled_late else None
    overdue_days = (today - deadline).days if deadline and today > deadline else None

    # Clock state. Settled within tolerance AND within the window clears the 180-day
    # pressure; settled after the deadline clears the bond but leaves a penalty payable.
    if start is None:
        state = "NO_ETA"
    elif settled and settled_late:
        state = "CLEARED_LATE"
    elif settled:
        state = "CLEARED"
    elif days is not None and days < 0:
        state = "PASSED"
    elif days is not None and days <= BOND_WARN_LEAD:
        state = "DUE_SOON"
    else:
        state = "OK"

    # A penalty is owed while the bond sits past its deadline, and stays owed if the
    # settling lifting landed late. Amount is never derived — it is read off the document
    # or entered by the user with a reason.
    penalty_applies = state in ("PASSED", "CLEARED_LATE")
    penalty_recorded = gd.bond_penalty_pkr is not None
    penalty_days = settled_late_by if settled_late else overdue_days

    return {
        "applies": True,
        "bonded_qty_mt": _f(bonded),
        "bonded_basis": "gross_weight",
        "lifted_qty_mt": _f(lifted),
        "remaining_qty_mt": _f(remaining),
        "over_lifted": over,
        "over_ex_bond_mt": _f(over_amt if over_amt > 0 else Decimal("0")),
        "tolerance_mt": _f(tol),
        "tolerance_pct": float(BOND_SETTLE_TOLERANCE_PCT * 100),
        "is_weight_settled": settled,
        "can_complete": settled,
        "fulfilled_pct": fulfilled_pct,
        "ib_start_date": start.isoformat() if start else None,
        "settlement_date": settled_on.isoformat() if settled_on else None,
        "into_bond_gd_uploaded": bool(getattr(gd, "into_bond_gd_uploaded", False)),
        "eta": gd.eta.isoformat() if gd.eta else None,
        "deadline": deadline.isoformat() if deadline else None,
        "days_remaining": days,
        "days_elapsed": days_elapsed,
        "timer_pct": timer_pct,
        "state": state,
        "bond_days": INTO_BOND_DAYS,
        "warn_lead_days": BOND_WARN_LEAD,
        "ex_bond_count": len(entries),
        # 180-day breach + penalty
        "settled_late": settled_late,
        "settled_late_by_days": settled_late_by,
        "overdue_days": overdue_days,
        "penalty_applies": penalty_applies,
        "penalty_recorded": penalty_recorded,
        "penalty_days": penalty_days,
        "penalty_pkr": _f(gd.bond_penalty_pkr),
        "penalty_source": gd.bond_penalty_source,
        "penalty_reason": gd.bond_penalty_reason,
        # A penalty recorded on an assumed breach that a later back-dated Ex-Bond
        # disproves. The UI shows it struck through; the SRO/report totals ignore it.
        "penalty_stale": penalty_recorded and not penalty_applies,
        "entries": [{
            "entry_id": e.entry_id, "gd_number": e.gd_number,
            "ex_bond_date": e.ex_bond_date.isoformat() if e.ex_bond_date else None,
            "quantity_mt": _f(e.quantity_mt), "remarks": e.remarks,
            # Duty paid to lift this parcel — feeds "GD previously paid" on the report.
            "duties_paid_pkr": _f(e.duties_paid_pkr),
            "duty_source": e.duty_source,
            "attachment_id": e.attachment_id,
            "filename": getattr(att_map.get(e.attachment_id), "filename", None),
            "source": "document" if e.attachment_id else "manual",
            # Partial GD (business term) — present once this release went through the
            # per-release GD View + Item Details + approval-validation workflow.
            "is_partial_gd": bool(partial_atts_by_entry.get(e.entry_id) or e.approval_id),
            "is_finalized": bool(e.is_finalized),
            "approval_id": e.approval_id,
            "approval_label": approval_labels.get(e.approval_id),
            "matched_sro_no": e.matched_sro_no,
            "item_details_count": partial_item_counts.get(e.entry_id, 0),
            "attachments": partial_atts_by_entry.get(e.entry_id, []),
        } for e in entries],
    }


def ex_bond_would_exceed(gd: GoodsDeclaration, db: Session, qty, exclude_entry_id=None):
    """How much a new/edited ex-bond lifting would overshoot the bonded quantity.
    Within the 1–2% settle tolerance is allowed without force.
    Returns (exceeds: bool, remaining_before: float, over_by: float)."""
    bonded = resolve_bonded_qty(gd)
    q = db.query(ExBondEntry).filter(ExBondEntry.into_bond_gd_id == gd.gd_id)
    if exclude_entry_id:
        q = q.filter(ExBondEntry.entry_id != exclude_entry_id)
    lifted = sum((_d(e.quantity_mt) for e in q.all()), Decimal("0"))
    # No bonded quantity recorded yet -> nothing to check against.
    if bonded is None or bonded <= 0:
        remaining = Decimal("0") - lifted
        return False, _f(remaining), 0.0
    remaining = bonded - lifted
    new_total = lifted + _d(qty)
    over = new_total - bonded
    tol = settle_tolerance_mt(bonded)
    if over <= tol:
        return False, _f(remaining), _f(over if over > 0 else Decimal("0"))
    return True, _f(remaining), _f(over)


# ---------------------------------------------------------------------------
# Cross-checks (advisory — same style as shipment_validator.cross_check_gd_extracted)
# ---------------------------------------------------------------------------
def _primary(items):
    if not items:
        return None
    return sorted(items, key=lambda d: (
        1 if getattr(d, "status", None) == "PENDING_REVIEW" else 0,
        -(d.created_at.timestamp() if getattr(d, "created_at", None) else 0),
    ))[0]


def _weights_of(shipment: Shipment):
    """Every weight (MT) we can compare a WeBOC document against, labelled by source."""
    out = []
    bl = _primary(shipment.bill_of_ladings)
    inv = _primary(shipment.commercial_invoices)
    pkg = _primary(shipment.packing_lists)
    if bl and bl.gross_weight_mt is not None:
        out.append(("BL gross weight", float(bl.gross_weight_mt)))
    if inv and inv.total_net_weight_mt is not None:
        out.append(("Invoice net weight", float(inv.total_net_weight_mt)))
    if pkg and pkg.total_net_weight_mt is not None:
        out.append(("Packing List net weight", float(pkg.total_net_weight_mt)))
    if shipment.total_net_weight_mt is not None:
        out.append(("Shipment net weight", float(shipment.total_net_weight_mt)))
    elif shipment.expected_quantity_mt is not None:
        out.append(("Shipment expected quantity", float(shipment.expected_quantity_mt)))
    return out


def cross_check_gd_view(extracted: dict, shipment_id: int, db: Session) -> list:
    """Advisory warnings for a freshly extracted GD View. Nothing is blocked."""
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        return []
    e = extracted or {}
    warnings = []
    lc = db.query(LCMaster).filter(LCMaster.lc_id == s.lc_id).first() if s.lc_id else None
    bl = _primary(s.bill_of_ladings)

    # Declaration type must be readable — an unknown type blocks the into/ex-bond workflow.
    if classify_declaration(e.get("declaration_type")) == "UNKNOWN":
        warnings.append(
            f"Declaration type could not be determined"
            f"{' from ' + repr(e.get('declaration_type')) if e.get('declaration_type') else ''}. "
            f"Set it manually (Into Bond / Ex Bond / Home Consumption) — it drives the bond workflow.")

    # BL number vs the shipment's BL
    gd_bl = e.get("bl_number")
    ship_bl = (bl.bl_number if bl else None) or (lc.bl_number if lc else None)
    if gd_bl and ship_bl and _lenient_match(gd_bl, ship_bl, "reference") is False:
        warnings.append(f"GD View B/L number '{gd_bl}' does not match the shipment's "
                        f"Bill of Lading '{ship_bl}'.")

    # Importer / company vs the LC
    gd_imp = e.get("importer_name")
    if gd_imp and lc and lc.importer_name and _lenient_match(gd_imp, lc.importer_name, "company") is False:
        warnings.append(f"GD View importer '{gd_imp}' does not match the LC's importer "
                        f"'{lc.importer_name}'.")

    # ETA vs the shipment ETA
    gd_eta = e.get("eta")
    if gd_eta and s.eta:
        try:
            eta_d = date.fromisoformat(str(gd_eta)[:10])
            diff = abs((eta_d - s.eta).days)
            if diff > 3:
                warnings.append(f"GD View ETA {eta_d} differs from the shipment ETA {s.eta} "
                                f"by {diff} day(s). The 18-day GD filing deadline and the 180-day "
                                f"bond deadline are both measured from this ETA.")
        except ValueError:
            pass

    # Weight vs the other documents
    gd_wt = e.get("net_weight_mt") or e.get("gross_weight_mt")
    if gd_wt is not None:
        try:
            gd_wt = float(gd_wt)
            for label, val in _weights_of(s):
                if abs(gd_wt - val) > WEIGHT_TOLERANCE_MT:
                    warnings.append(f"GD View weight {gd_wt:.3f} MT differs from the "
                                    f"{label} {val:.3f} MT (by {abs(gd_wt-val):.3f} MT).")
        except (TypeError, ValueError):
            pass

    # Section 82 penalty -> the GD was filed late.
    pen = e.get("section82_penalty_pkr")
    if pen:
        try:
            if float(pen) > 0:
                warnings.append(f"Section 82 penalty of PKR {float(pen):,.0f} found on this GD View — "
                                f"the GD appears to have been LATE FILED. It will be recorded as such.")
        except (TypeError, ValueError):
            pass

    return warnings


def cross_check_item_details(extracted: dict, shipment_id: int, db: Session) -> list:
    """Advisory warnings for a freshly extracted Item Details document."""
    s = db.query(Shipment).filter(Shipment.shipment_id == shipment_id).first()
    if not s:
        return []
    e = extracted or {}
    warnings = []
    lc = db.query(LCMaster).filter(LCMaster.lc_id == s.lc_id).first() if s.lc_id else None
    gd = (db.query(GoodsDeclaration)
            .filter(GoodsDeclaration.shipment_id == shipment_id)
            .order_by(GoodsDeclaration.gd_id.desc()).first())
    items = e.get("items") or []

    # Company vs the LC
    if e.get("importer_name") and lc and lc.importer_name and \
            _lenient_match(e["importer_name"], lc.importer_name, "company") is False:
        warnings.append(f"Item Details importer '{e['importer_name']}' does not match the LC's "
                        f"importer '{lc.importer_name}'.")

    # HS codes vs the LC products (item category / HS on the LC)
    lc_hs = {_norm_hs(p.hs_code) for p in (lc.products if lc else []) if p.hs_code}
    lc_hs = {h for h in lc_hs if h}
    if lc_hs:
        for it in items:
            h = _norm_hs(it.get("hs_code"))
            if h and h not in lc_hs:
                warnings.append(f"Item {it.get('item_number') or ''} HS code "
                                f"'{it.get('hs_code')}' is not on the LC "
                                f"(LC HS: {', '.join(sorted(lc_hs))}).")

    # Quantity/weight vs the GD View and the shipment documents
    total = e.get("total_quantity")
    if total is None and items:
        try:
            total = sum(float(i.get("quantity") or 0) for i in items)
        except (TypeError, ValueError):
            total = None
    unit = (e.get("total_unit") or (items[0].get("unit") if items else None) or "").upper()
    total_mt = None
    if total is not None:
        try:
            total = float(total)
            total_mt = total / 1000.0 if unit.startswith("KG") else total
        except (TypeError, ValueError):
            total_mt = None
    if total_mt is None and e.get("net_weight_mt") is not None:
        try:
            total_mt = float(e["net_weight_mt"])
        except (TypeError, ValueError):
            total_mt = None

    if total_mt is not None:
        gd_weight = (gd.net_weight_mt or gd.gross_weight_mt) if gd else None
        if gd_weight is not None:
            gdw = float(gd_weight)
            if abs(total_mt - gdw) > WEIGHT_TOLERANCE_MT:
                warnings.append(f"Item Details total quantity {total_mt:.3f} MT differs from the "
                                f"GD View weight {gdw:.3f} MT (by {abs(total_mt-gdw):.3f} MT).")
        for label, val in _weights_of(s):
            if abs(total_mt - val) > WEIGHT_TOLERANCE_MT:
                warnings.append(f"Item Details total quantity {total_mt:.3f} MT differs from the "
                                f"{label} {val:.3f} MT (by {abs(total_mt-val):.3f} MT).")

    if items and not any(it.get("sro_no") or it.get("quota_reference") for it in items):
        warnings.append("No SRO / quota reference was found on any item — SRO quota usage "
                        "cannot be tracked from this document.")

    return warnings
