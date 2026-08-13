"""Shipment journey + deadline timeline — workflow layer for importer UX."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models.database_models import (
    BillOfLading, GDAttachment, GoodsDeclaration, LCMaster, Shipment,
)
from modules.shipments import services as ship_svc
from modules.shipments.demurrage_service import compute_demurrage
from modules.shipments.bl_service import get_demurrage_config
from modules.weboc.gd_service import _has_attachment
from modules.weboc.helpers.weboc_service import filing_deadline, bond_summary, GD_FILING_DAYS


def _tone_from_days(days: Optional[int], *, warn_lead: int = 10) -> str:
    if days is None:
        return "unknown"
    if days < 0:
        return "overdue"
    if days <= warn_lead:
        return "due"
    return "ok"


def _step(
    *,
    step_id: str,
    label: str,
    status: str,
    expected_by: Optional[str] = None,
    blocker: Optional[str] = None,
    action_label: Optional[str] = None,
    action_href: Optional[str] = None,
    report_href: Optional[str] = None,
    branch: Optional[str] = None,
) -> dict:
    return {
        "id": step_id,
        "label": label,
        "status": status,
        "expected_by": expected_by,
        "blocker": blocker,
        "action_label": action_label,
        "action_href": action_href,
        "report_href": report_href,
        "branch": branch,
    }


def _doc_upload_href(shipment_id: int, lc_id: int, doc_type: str, **extra) -> str:
    params = f"shipment_id={shipment_id}&lc_id={lc_id}&type={doc_type}"
    for k, v in extra.items():
        if v is not None:
            params += f"&{k}={v}"
    return f"/shipment-doc-upload?{params}"


def _validation_blocking(s: Shipment) -> tuple[bool, Optional[str]]:
    fails = [v for v in (s.validations or []) if (v.status or "").upper() == "FAIL"]
    if fails:
        return True, f"{len(fails)} validation check(s) failed — resolve discrepancies before proceeding"
    if (s.validation_status or "").upper() == "DISCREPANT":
        return True, "Shipment has document discrepancies — run validation and fix issues"
    return False, None


def build_journey(shipment_id: int, db: Session) -> dict:
    s = ship_svc.get_shipment_or_404(shipment_id, db, options=[
        joinedload(Shipment.lc),
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.commercial_invoices),
        joinedload(Shipment.packing_lists),
        joinedload(Shipment.goods_declarations),
        joinedload(Shipment.financial_instruments),
        joinedload(Shipment.insurance_certificates),
        joinedload(Shipment.validations),
    ])
    lc_id = s.lc_id
    lc = s.lc
    today = date.today()
    steps: list[dict] = []

    has_bl = bool(s.bill_of_ladings)
    has_inv = bool(s.commercial_invoices)
    has_pkg = bool(s.packing_lists)
    has_ins = bool(s.insurance_certificates)
    has_fi = bool(s.financial_instruments)
    docs_core_done = has_bl and has_inv and has_pkg

    val_blocked, val_msg = _validation_blocking(s)
    eta = s.eta
    eta_days = (eta - today).days if eta else None
    doc_expected = None
    if eta:
        doc_expected = (eta - timedelta(days=3)).isoformat()

    steps.append(_step(
        step_id="lc_linked",
        label="LC Linked",
        status="done",
        action_href=f"/lc-detail?id={lc_id}" if lc_id else None,
    ))

    core_status = "done" if docs_core_done else ("due" if has_bl or has_inv or has_pkg else "blocked")
    missing = []
    if not has_bl:
        missing.append("BL")
    if not has_inv:
        missing.append("Invoice")
    if not has_pkg:
        missing.append("Packing List")
    steps.append(_step(
        step_id="docs_core",
        label="Core Documents (BL, Invoice, Packing)",
        status=core_status if not (eta_days is not None and eta_days <= 0 and not docs_core_done) else "overdue",
        expected_by=doc_expected,
        blocker=f"Missing: {', '.join(missing)}" if missing else None,
        action_label="Upload next document" if missing else None,
        action_href=_doc_upload_href(
            shipment_id, lc_id,
            "bl" if not has_bl else ("invoice" if not has_inv else "packing"),
        ) if missing else f"/shipment?id={shipment_id}&tab=documents",
        report_href="/demurrage-report",
    ))

    val_status = "blocked" if val_blocked else ("done" if docs_core_done and not val_blocked else "blocked")
    if docs_core_done and not val_blocked:
        val_status = "done" if (s.validation_status or "").upper() in ("ALL_CLEAR", "VALIDATED") else "due"
    steps.append(_step(
        step_id="docs_validated",
        label="Documents Validated",
        status=val_status if docs_core_done else "blocked",
        blocker=val_msg if docs_core_done and val_blocked else ("Upload core documents first" if not docs_core_done else None),
        action_label="Run validation" if docs_core_done and val_status != "done" else None,
        action_href=f"/shipment?id={shipment_id}&tab=validation",
    ))

    vessel_status = "done" if eta else ("due" if docs_core_done else "blocked")
    steps.append(_step(
        step_id="vessel_tracked",
        label="Vessel ETA Known",
        status=vessel_status,
        expected_by=eta.isoformat() if eta else None,
        action_label="Set ETA" if not eta else None,
        action_href=f"/shipment?id={shipment_id}&tab=overview",
        report_href="/vessel-tracking",
    ))

    fi = ship_svc.ordered_docs(s.financial_instruments)[0] if s.financial_instruments else None
    fi_expiry = fi.expiry_date.isoformat() if fi and fi.expiry_date else None
    fi_status = "done" if has_fi else "due"
    steps.append(_step(
        step_id="fyi_uploaded",
        label="Financial Instrument (FYI)",
        status=fi_status,
        expected_by=fi_expiry,
        blocker=None if has_fi else "FYI required before GD filing in most cases",
        action_label="Upload FYI" if not has_fi else None,
        action_href=_doc_upload_href(shipment_id, lc_id, "fi"),
    ))

    gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
    gd_view = gd and (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded or gd.gd_number)
    item_details = gd and _has_attachment(gd.gd_id, "ITEM_DETAILS", db)
    final_gd = gd and (_has_attachment(gd.gd_id, "FINAL_GD", db) or gd.final_gd_uploaded)

    gd_started_status = "done" if gd_view else ("due" if docs_core_done else "blocked")
    fd = filing_deadline(gd, today) if gd else {}
    gd_deadline = fd.get("deadline")
    steps.append(_step(
        step_id="gd_started",
        label="GD View Filed",
        status="overdue" if gd and not gd_view and fd.get("state") == "OVERDUE" else gd_started_status,
        expected_by=gd_deadline,
        blocker="Upload GD View after core documents" if not gd_view and docs_core_done else None,
        action_label="Upload GD View" if not gd_view else None,
        action_href=_doc_upload_href(shipment_id, lc_id, "gdview") if not gd_view else f"/shipment?id={shipment_id}&tab=customs",
        report_href="/gd-report",
    ))

    gd_type = (gd.gd_type or "").upper() if gd else ""
    clearance_chosen = gd_type in ("HOME_CONSUMPTION", "INTO_BOND", "EX_BOND")

    steps.append(_step(
        step_id="clearance_path",
        label="Clearance Path (Home Consumption or Into-Bond)",
        status="done" if clearance_chosen else ("due" if gd_view else "blocked"),
        action_label="Choose clearance path" if gd_view and not clearance_chosen else None,
        action_href=f"/shipment?id={shipment_id}&tab=customs",
    ))

    hc_status = "blocked"
    ib_status = "blocked"
    if gd_view and clearance_chosen:
        if gd_type == "HOME_CONSUMPTION":
            hc_status = "done" if final_gd else ("due" if item_details else "blocked")
            ib_status = "blocked"
        elif gd_type == "INTO_BOND":
            ib_gd = gd and _has_attachment(gd.gd_id, "INTO_BOND_GD", db)
            bond = bond_summary(gd, db, today) if gd else {}
            ib_settled = bond.get("is_weight_settled", False)
            ib_status = "done" if ib_settled else ("due" if ib_gd else "blocked")
            hc_status = "blocked"
        else:
            hc_status = "due"
            ib_status = "due"

    steps.append(_step(
        step_id="gd_hc",
        label="Home Consumption → Final GD",
        status=hc_status,
        branch="home_consumption",
        blocker="Upload Item Details first" if gd_type == "HOME_CONSUMPTION" and gd_view and not item_details else None,
        action_label="Upload Final GD" if gd_type == "HOME_CONSUMPTION" and item_details and not final_gd else (
            "Upload Item Details" if gd_type == "HOME_CONSUMPTION" and gd_view and not item_details else None
        ),
        action_href=_doc_upload_href(shipment_id, lc_id, "itemdetails") if gd_type == "HOME_CONSUMPTION" and gd_view and not item_details else (
            _doc_upload_href(shipment_id, lc_id, "gd", final="1") if gd_type == "HOME_CONSUMPTION" and item_details and not final_gd else None
        ),
        report_href="/gd-report",
    ))

    bond = bond_summary(gd, db, today) if gd and gd_type == "INTO_BOND" else {}
    ib_deadline = bond.get("deadline")
    steps.append(_step(
        step_id="gd_ib",
        label="Into-Bond → Ex-Bond Release",
        status=ib_status,
        branch="into_bond",
        expected_by=ib_deadline,
        blocker="Upload Into-Bond GD first" if gd_type == "INTO_BOND" and gd_view and not _has_attachment(gd.gd_id, "INTO_BOND_GD", db) else None,
        action_label="Manage Ex-Bond" if gd_type == "INTO_BOND" and bond.get("applies") and not bond.get("is_weight_settled") else (
            "Upload Into-Bond GD" if gd_type == "INTO_BOND" and gd_view else None
        ),
        action_href=f"/shipment?id={shipment_id}&tab=customs",
        report_href="/gd-report",
    ))

    if not has_ins:
        steps.append(_step(
            step_id="insurance",
            label="Insurance Certificate",
            status="due",
            action_label="Upload Insurance",
            action_href=_doc_upload_href(shipment_id, lc_id, "insurance"),
        ))

    done_count = sum(1 for st in steps if st["status"] == "done")
    total = len(steps)
    pct = round(100 * done_count / total) if total else 0

    return {
        "shipment_id": shipment_id,
        "lc_id": lc_id,
        "lc_number": lc.lc_number if lc else None,
        "completeness_pct": pct,
        "steps": steps,
        "clearance_type": gd_type or None,
        "validation_blocked": val_blocked,
    }


def build_timeline(shipment_id: int, db: Session) -> dict:
    s = ship_svc.get_shipment_or_404(shipment_id, db, options=[
        joinedload(Shipment.lc),
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.financial_instruments),
        joinedload(Shipment.goods_declarations),
    ])
    lc = s.lc
    today = date.today()
    markers: list[dict] = []

    def add_marker(key: str, label: str, d: Optional[date], *, warn_lead: int = 10,
                   href: Optional[str] = None, note: Optional[str] = None):
        if not d:
            return
        days = (d - today).days
        markers.append({
            "key": key,
            "label": label,
            "date": d.isoformat(),
            "days_remaining": days,
            "tone": _tone_from_days(days, warn_lead=warn_lead),
            "href": href,
            "note": note,
        })

    if lc:
        add_marker("lc_expiry", "LC Expiry", lc.expiry_date, warn_lead=14,
                   href=f"/lc-detail?id={lc.lc_id}", note="Letter of Credit expiry")
        add_marker("last_ship", "Latest Shipment", lc.last_ship_date, warn_lead=10,
                   href=f"/lc-detail?id={lc.lc_id}")

    add_marker("eta", "Vessel ETA", s.eta, warn_lead=3,
               href=f"/shipment?id={shipment_id}", note="Estimated arrival at port")

    gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
    if gd:
        fd = filing_deadline(gd, today)
        if fd.get("deadline") and not fd.get("filed"):
            add_marker("gd_filing", f"GD Filing (ETA+{GD_FILING_DAYS})", date.fromisoformat(fd["deadline"][:10]),
                       warn_lead=10, href=f"/shipment?id={shipment_id}&tab=customs",
                       note=f"File GD within {GD_FILING_DAYS} days of ETA")
        bond = bond_summary(gd, db, today)
        if bond.get("applies") and bond.get("deadline") and not bond.get("is_weight_settled"):
            add_marker("into_bond", "Into-Bond 180-Day", date.fromisoformat(str(bond["deadline"])[:10]),
                       warn_lead=20, href=f"/shipment?id={shipment_id}&tab=customs",
                       note="Settle bonded cargo via Ex-Bond")

    fi = ship_svc.ordered_docs(s.financial_instruments)[0] if s.financial_instruments else None
    if fi and fi.expiry_date:
        add_marker("fyi_expiry", "FYI Expiry", fi.expiry_date, warn_lead=7,
                   href=_doc_upload_href(shipment_id, s.lc_id, "fi"),
                   note="Last date to file GD against FYI")

    config = get_demurrage_config(db)
    for bl in ship_svc.ordered_docs(s.bill_of_ladings):
        dem = compute_demurrage(bl, config, today)
        lfd = dem.get("last_free_date")
        if lfd:
            lfd_date = date.fromisoformat(str(lfd)[:10]) if isinstance(lfd, str) else lfd
            add_marker(f"demurrage_{bl.bl_id}", f"Demurrage LFD ({bl.bl_number or 'BL'})",
                       lfd_date, warn_lead=3, href="/demurrage-report",
                       note="Last free day before demurrage accrues")

    markers.sort(key=lambda m: m["date"])
    return {"shipment_id": shipment_id, "today": today.isoformat(), "markers": markers}


def lc_completeness(lc_id: int, db: Session) -> dict:
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        return {"lc_id": lc_id, "completeness_pct": 0, "shipment_count": 0, "shipments": []}

    shipments = db.query(Shipment).filter(
        Shipment.lc_id == lc_id, Shipment.is_deleted.is_(False),
    ).all()
    if not shipments:
        has_contract = bool(lc.contract_id)
        base = 20 if lc.lc_number else 0
        base += 20 if has_contract else 0
        return {
            "lc_id": lc_id,
            "lc_number": lc.lc_number,
            "completeness_pct": base,
            "shipment_count": 0,
            "shipments": [],
            "next_action": "Create first shipment",
            "next_href": f"/lc-detail?id={lc_id}",
        }

    journey_rows = []
    pcts = []
    for sh in shipments:
        j = build_journey(sh.shipment_id, db)
        pcts.append(j["completeness_pct"])
        journey_rows.append({
            "shipment_id": sh.shipment_id,
            "shipment_ref": sh.shipment_ref,
            "completeness_pct": j["completeness_pct"],
        })

    avg = round(sum(pcts) / len(pcts)) if pcts else 0
    lowest = min(journey_rows, key=lambda x: x["completeness_pct"]) if journey_rows else None
    next_action = None
    next_href = None
    if lowest and lowest["completeness_pct"] < 100:
        next_action = f"Continue {lowest.get('shipment_ref') or 'shipment'}"
        next_href = f"/shipment?id={lowest['shipment_id']}"

    return {
        "lc_id": lc_id,
        "lc_number": lc.lc_number,
        "completeness_pct": avg,
        "shipment_count": len(shipments),
        "shipments": journey_rows,
        "next_action": next_action,
        "next_href": next_href,
    }


def completeness_pct_light(s: Shipment, db: Session) -> int:
    """Fast doc-milestone completeness without full journey step list."""
    score = 1  # LC linked (shipment exists)
    total = 8
    if s.bill_of_ladings:
        score += 1
    if s.commercial_invoices:
        score += 1
    if s.packing_lists:
        score += 1
    if s.eta:
        score += 1
    if s.financial_instruments:
        score += 1
    gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
    if gd and (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded or gd.gd_number):
        score += 1
    if gd:
        if (gd.gd_type or "") == "INTO_BOND":
            bond = bond_summary(gd, db)
            if bond.get("is_weight_settled"):
                score += 1
        elif _has_attachment(gd.gd_id, "FINAL_GD", db) or gd.final_gd_uploaded:
            score += 1
    return round(100 * score / total)


def completeness_pct_for_shipment(s: Shipment, db: Session) -> int:
    """Lightweight completeness % without building full journey steps."""
    return completeness_pct_light(s, db)


def _doc_status_for_shipment(s: Shipment, doc_type: str, present: bool, db: Session,
                             gd: Optional[GoodsDeclaration] = None) -> str:
    if not present:
        return "missing"
    ship_val = (s.validation_status or "").upper()
    if ship_val == "DISCREPANT":
        return "discrepant"
    fails = [v for v in (s.validations or []) if (v.status or "").upper() == "FAIL"]
    if fails and doc_type in ("bl", "invoice", "packing"):
        return "discrepant"
    if ship_val in ("ALL_CLEAR", "VALIDATED"):
        return "validated"
    return "uploaded"


def build_doc_status(shipment_id: int, db: Session) -> dict:
    s = ship_svc.get_shipment_or_404(shipment_id, db, options=[
        joinedload(Shipment.lc),
        joinedload(Shipment.bill_of_ladings),
        joinedload(Shipment.commercial_invoices),
        joinedload(Shipment.packing_lists),
        joinedload(Shipment.goods_declarations),
        joinedload(Shipment.financial_instruments),
        joinedload(Shipment.insurance_certificates),
        joinedload(Shipment.validations),
    ])
    today = date.today()
    lc_id = s.lc_id
    eta = s.eta
    doc_expected_core = (eta - timedelta(days=3)).isoformat() if eta else None
    has_bl = bool(s.bill_of_ladings)
    has_inv = bool(s.commercial_invoices)
    has_pkg = bool(s.packing_lists)
    docs_core_done = has_bl and has_inv and has_pkg
    eta_days = (eta - today).days if eta else None
    whatsapp_critical = eta_days is not None and eta_days <= 3 and not docs_core_done

    gd = ship_svc.ordered_docs(s.goods_declarations)[0] if s.goods_declarations else None
    gd_view = gd and (_has_attachment(gd.gd_id, "GD_VIEW", db) or gd.gd_view_uploaded or gd.gd_number)
    item_details = gd and _has_attachment(gd.gd_id, "ITEM_DETAILS", db)
    final_gd = gd and (_has_attachment(gd.gd_id, "FINAL_GD", db) or gd.final_gd_uploaded)

    fi = ship_svc.ordered_docs(s.financial_instruments)[0] if s.financial_instruments else None
    fi_expiry = fi.expiry_date.isoformat() if fi and fi.expiry_date else None

    gd_filing_deadline = None
    if gd:
        fd = filing_deadline(gd, today)
        if fd.get("deadline"):
            gd_filing_deadline = fd["deadline"][:10]

    def doc_row(doc_type: str, label: str, present: bool, expected_by: Optional[str]) -> dict:
        status = _doc_status_for_shipment(s, doc_type, present, db, gd)
        critical = whatsapp_critical and doc_type in ("bl", "invoice", "packing") and not present
        return {
            "type": doc_type,
            "label": label,
            "status": status,
            "expected_by": expected_by,
            "whatsapp_critical": critical,
            "upload_href": _doc_upload_href(shipment_id, lc_id, doc_type) if doc_type != "gd" else
                           _doc_upload_href(shipment_id, lc_id, "gd", final="1"),
        }

    docs = [
        doc_row("bl", "Bill of Lading", has_bl, doc_expected_core),
        doc_row("invoice", "Commercial Invoice", has_inv, doc_expected_core),
        doc_row("packing", "Packing List", has_pkg, doc_expected_core),
        doc_row("insurance", "Insurance", bool(s.insurance_certificates), doc_expected_core),
        doc_row("fi", "Financial Instrument", bool(s.financial_instruments), fi_expiry),
        doc_row("gdview", "GD View", bool(gd_view), gd_filing_deadline),
        doc_row("itemdetails", "Item Details", bool(item_details), gd_filing_deadline),
        doc_row("gd", "Final GD", bool(final_gd), gd_filing_deadline),
    ]

    return {
        "shipment_id": shipment_id,
        "lc_id": lc_id,
        "eta": eta.isoformat() if eta else None,
        "whatsapp_critical": whatsapp_critical,
        "docs": docs,
    }
