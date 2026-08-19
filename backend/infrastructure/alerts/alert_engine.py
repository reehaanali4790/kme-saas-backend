"""
Operational Alert Engine.
Scans LCs, shipments, and GDs and generates SystemAlert records for events that
need human attention. Idempotent: re-scanning won't duplicate an active alert
(deduped by dedup_key). This is the brain behind the dashboard "Needs Attention"
panel and WhatsApp notifications.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from models.database_models import (
    LCMaster, LCProduct, Shipment, GoodsDeclaration, SystemAlert,
    BillOfLading, ContainerEvent, DemurrageConfig, FinancialInstrument, EdbApproval,
)
from config.settings import settings
from modules.weboc.helpers.bond_alerts import scan_bond_alerts
from modules.shipments.demurrage_service import compute_demurrage
from infrastructure.normalization.normalization_service import payment_tenor
from modules.weboc.helpers.weboc_service import (
    filing_deadline, bond_summary, GD_FILING_DAYS, INTO_BOND_DAYS,
)
from modules.weboc.helpers.sro_usage import compute_usage

logger = logging.getLogger("uvicorn")

# Thresholds (tunable)
LC_EXPIRY_DAYS = 14          # warn when LC expires within N days
LAST_SHIP_DAYS = 10          # warn when last ship date within N days
VARIANCE_TOLERANCE_MT = 0.5  # short-shipment threshold
DUTY_SURPRISE_PCT = 10.0     # assessed value exceeds declared by this % => alert
MISSING_DOC_ETA_DAYS = 10    # vessel arriving within N days with incomplete docs => HIGH alert
DA_DOC_ARRIVAL_DAYS = 5      # DA LC: docs expected within N days of BL / shipped-on-board
ARRIVAL_DOCS_LEAD_DAYS = 3   # vessel arriving within N days with incomplete docs => WhatsApp-critical alert


def _active_keys(db: Session) -> set:
    rows = db.query(SystemAlert.dedup_key).filter(SystemAlert.status == "ACTIVE").all()
    return {r[0] for r in rows if r[0]}


def _add(db, created, existing, *, key, atype, severity, title, message,
         entity_type, entity_id, lc_id=None, shipment_id=None):
    if key in existing:
        return
    db.add(SystemAlert(
        alert_type=atype, severity=severity, title=title, message=message,
        entity_type=entity_type, entity_id=entity_id, lc_id=lc_id,
        shipment_id=shipment_id, dedup_key=key, status="ACTIVE",
    ))
    existing.add(key)
    created.append(atype)


def scan(db: Session) -> dict:
    """Run all alert rules. Returns counts by type of NEW alerts created."""
    today = date.today()
    existing = _active_keys(db)
    created = []

    # ---- 1. LC expiring soon ----
    open_lcs = db.query(LCMaster).filter(
        LCMaster.status.in_(["OPEN", "SHIPPED"]),
        LCMaster.expiry_date.isnot(None),
    ).all()
    for lc in open_lcs:
        days = (lc.expiry_date - today).days
        if 0 <= days <= LC_EXPIRY_DAYS:
            sev = "HIGH" if days <= 5 else "MEDIUM"
            _add(db, created, existing,
                 key=f"LC_EXPIRING:{lc.lc_id}:{lc.expiry_date}",
                 atype="LC_EXPIRING", severity=sev,
                 title=f"LC {lc.lc_number} expires in {days} day(s)",
                 message=f"LC {lc.lc_number} expires on {lc.expiry_date}. "
                         f"Ensure all shipments and documents are completed in time.",
                 entity_type="LC", entity_id=lc.lc_id, lc_id=lc.lc_id)
        # ---- 2. Last ship date approaching ----
        if lc.last_ship_date:
            lsd = (lc.last_ship_date - today).days
            if 0 <= lsd <= LAST_SHIP_DAYS:
                _add(db, created, existing,
                     key=f"LAST_SHIP_DATE:{lc.lc_id}:{lc.last_ship_date}",
                     atype="LAST_SHIP_DATE", severity="HIGH",
                     title=f"Last ship date in {lsd} day(s) — LC {lc.lc_number}",
                     message=f"The last shipment date for LC {lc.lc_number} is {lc.last_ship_date}. "
                             f"Goods must be shipped before this date.",
                     entity_type="LC", entity_id=lc.lc_id, lc_id=lc.lc_id)

    # ---- 3 & 4 & 5. Shipment-level: short shipment / discrepancy / missing docs ----
    shipments = db.query(Shipment).filter(Shipment.is_deleted.is_(False)).all()
    for s in shipments:
        # short shipment
        if s.quantity_variance_mt is not None and s.quantity_variance_mt < Decimal(str(-VARIANCE_TOLERANCE_MT)):
            short = abs(float(s.quantity_variance_mt))
            _add(db, created, existing,
                 key=f"SHORT_SHIPMENT:{s.shipment_id}",
                 atype="SHORT_SHIPMENT", severity="HIGH",
                 title=f"Short shipment on {s.shipment_ref or 'SH-'+str(s.shipment_id)}",
                 message=f"Delivered quantity is short by {short:.3f} MT versus expected "
                         f"(LC {s.lc.lc_number if s.lc else ''}). Possible supplier short-shipment.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

        # discrepancy
        if s.validation_status == "DISCREPANT":
            _add(db, created, existing,
                 key=f"DISCREPANCY:{s.shipment_id}",
                 atype="DISCREPANCY", severity="MEDIUM",
                 title=f"Document discrepancy on {s.shipment_ref or 'SH-'+str(s.shipment_id)}",
                 message=f"Cross-document validation found mismatches for this shipment "
                         f"(LC {s.lc.lc_number if s.lc else ''}). Resolve before bank presentation.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

        # missing documents — incomplete BL+INV+PKG set.
        # Fires when the shipment already has at least one doc, OR when the vessel is
        # arriving soon (ETA within the lead window) but the papers are not yet complete —
        # an arriving vessel with no documents is the higher-risk case.
        has_bl = len(s.bill_of_ladings) > 0
        has_inv = len(s.commercial_invoices) > 0
        has_pkg = len(s.packing_lists) > 0
        any_doc = has_bl or has_inv or has_pkg
        full_set = has_bl and has_inv and has_pkg
        eta_days = (s.eta - today).days if s.eta else None
        eta_near = eta_days is not None and 0 <= eta_days <= MISSING_DOC_ETA_DAYS
        if not full_set and (any_doc or eta_near):
            missing = [n for n, h in [("BL", has_bl), ("Invoice", has_inv), ("Packing List", has_pkg)] if not h]
            sev = "HIGH" if eta_near else "MEDIUM"
            eta_note = (f" Vessel arrives on {s.eta} (ETA in {eta_days} day(s)) — clearance will stall "
                        f"without these documents." if eta_near else "")
            _add(db, created, existing,
                 key=f"MISSING_DOCUMENT:{s.shipment_id}:{'/'.join(missing)}",
                 atype="MISSING_DOCUMENT", severity=sev,
                 title=f"Missing {', '.join(missing)} on {s.shipment_ref or 'SH-'+str(s.shipment_id)}",
                 message=f"This shipment is missing: {', '.join(missing)}. "
                         f"Complete the document set to validate and clear.{eta_note}",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

        if (s.docs_reception_status or "") == "AWAITING":
            from modules.shipments.docs_reception import docs_reception_summary
            rec = docs_reception_summary(s)
            missing = rec.get("missing_required_docs") or []
            _add(db, created, existing,
                 key=f"LANDED_AWAITING_DOCS:{s.shipment_id}",
                 atype="LANDED_AWAITING_DOCS", severity="HIGH",
                 title=f"Vessel on port — documents awaiting on {s.shipment_ref or 'SH-'+str(s.shipment_id)}",
                 message=f"Shipment is on port but still missing: {', '.join(missing) or 'core documents'}.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

        # vessel arriving very soon (within ARRIVAL_DOCS_LEAD_DAYS) and docs still
        # incomplete — this is the WhatsApp-critical variant of the check above:
        # always fires regardless of any_doc, since an imminent arrival with any
        # gap in the document set risks a clearance delay.
        if eta_days is not None and 0 <= eta_days <= ARRIVAL_DOCS_LEAD_DAYS and not full_set:
            missing = [n for n, h in [("BL", has_bl), ("Invoice", has_inv), ("Packing List", has_pkg)] if not h]
            ref = s.shipment_ref or f"SH-{s.shipment_id:04d}"
            _add(db, created, existing,
                 key=f"ARRIVAL_DOCS_MISSING:{s.shipment_id}:{s.eta}",
                 atype="ARRIVAL_DOCS_MISSING", severity="HIGH",
                 title=f"Vessel arrives in {eta_days}d — {', '.join(missing)} still missing",
                 message=f"Shipment {ref} arrives on {s.eta} (in {eta_days} day(s)) and is still "
                         f"missing: {', '.join(missing)}. Prepare/upload these before arrival "
                         f"to avoid a clearance delay.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

        # ---- 12. DA LC document-arrival expectation ----
        # For a usance/DA LC, documents should arrive within DA_DOC_ARRIVAL_DAYS of the
        # BL / shipped-on-board date. Alert while the doc set is still incomplete.
        if s.bl_date and payment_tenor(s.lc.payment_terms if s.lc else None) == "DA":
            has_docs = (len(s.bill_of_ladings) > 0 and len(s.commercial_invoices) > 0
                        and len(s.packing_lists) > 0)
            if not has_docs:
                expected = s.bl_date + timedelta(days=DA_DOC_ARRIVAL_DAYS)
                overdue = (today - expected).days
                sev = "HIGH" if overdue >= 0 else ("MEDIUM" if overdue >= -3 else "LOW")
                ref = s.shipment_ref or f"SH-{s.shipment_id:04d}"
                when = ("was expected by" if overdue >= 0 else "is expected by")
                _add(db, created, existing,
                     key=f"DA_DOC_ARRIVAL:{s.shipment_id}:{expected}",
                     atype="DA_DOC_ARRIVAL", severity=sev,
                     title=f"DA LC documents {'overdue' if overdue >= 0 else 'due'} — {ref}",
                     message=f"This is a DA (usance) LC. Based on the BL date {s.bl_date}, the "
                             f"original documents {when} {expected} "
                             f"(BL date + {DA_DOC_ARRIVAL_DAYS} days). Follow up with the bank/supplier "
                             f"to ensure documents arrive as per the LC.",
                     entity_type="SHIPMENT", entity_id=s.shipment_id,
                     lc_id=s.lc_id, shipment_id=s.shipment_id)

    # ---- 6. Duty surprise (assessed >> declared) ----
    gds = db.query(GoodsDeclaration).filter(
        GoodsDeclaration.assessed_value_pkr.isnot(None),
        GoodsDeclaration.declared_value_pkr.isnot(None),
    ).all()
    for gd in gds:
        dec = float(gd.declared_value_pkr or 0)
        ass = float(gd.assessed_value_pkr or 0)
        if dec > 0 and ass > dec * (1 + DUTY_SURPRISE_PCT / 100.0):
            diff = ass - dec
            pct = (diff / dec) * 100
            _add(db, created, existing,
                 key=f"DUTY_SURPRISE:{gd.gd_id}",
                 atype="DUTY_SURPRISE", severity="HIGH",
                 title=f"Customs assessed {pct:.0f}% above declared — GD {gd.gd_number or gd.gd_id}",
                 message=f"Customs assessed PKR {ass:,.0f} vs declared PKR {dec:,.0f} "
                         f"(+PKR {diff:,.0f}). Prepare additional duty funds.",
                 entity_type="GD", entity_id=gd.gd_id, shipment_id=gd.shipment_id, lc_id=gd.lc_id)

    # ---- 8. Demurrage: free time running out / charges accruing ----
    dem_config = db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()
    dem_bls = db.query(BillOfLading).filter(
        BillOfLading.demurrage_start_date.isnot(None),
        BillOfLading.demurrage_cleared_date.is_(None),
    ).all()
    for bl in dem_bls:
        if bl.bl_type == "CONTAINER":
            continue
        dem = compute_demurrage(bl, dem_config, today)
        ref = bl.bl_number or f"BL-{bl.bl_id}"
        cur = dem["currency"]
        if dem["state"] == "ACCRUING":
            overdue = dem["chargeable_days"]
            total = dem.get("total_amount")
            amt = f" Total paid: {cur} {total:,.2f}." if total else ""
            _add(db, created, existing,
                 key=f"DEMURRAGE_ACCRUING:{bl.bl_id}",
                 atype="DEMURRAGE_ACCRUING", severity="HIGH",
                 title=f"Demurrage accruing on BL {ref} — {overdue} day(s) overdue",
                 message=f"BL {ref} passed its last free day ({dem['last_free_day']}). "
                         f"Demurrage has been accruing for {overdue} day(s).{amt} "
                         f"Take delivery, record the clearance date and enter the total amount paid.",
                 entity_type="BL", entity_id=bl.bl_id,
                 lc_id=bl.lc_id, shipment_id=bl.shipment_id)
        elif dem["state"] == "AT_RISK":
            left = dem["days_remaining"]
            sev = "HIGH" if left is not None and left <= 1 else "MEDIUM"
            _add(db, created, existing,
                 key=f"DEMURRAGE_WARN:{bl.bl_id}",
                 atype="DEMURRAGE_WARNING", severity=sev,
                 title=f"Demurrage free time ending on BL {ref} — {left} day(s) left",
                 message=f"BL {ref} reaches its last free day on {dem['last_free_day']} "
                         f"({left} day(s) away). After that demurrage will accrue until "
                         f"delivery is taken. Clear and take delivery in time.",
                 entity_type="BL", entity_id=bl.bl_id,
                 lc_id=bl.lc_id, shipment_id=bl.shipment_id)

    # ---- 9. Vessel arrival approaching (ETA within 10 days) ----
    ARRIVAL_LEAD_DAYS = 10
    arr_ships = db.query(Shipment).filter(
        Shipment.is_deleted.is_(False),
        Shipment.eta.isnot(None),
        Shipment.status.notin_(["PAYMENT_MADE", "ACCEPTED", "DELIVERED"]),
    ).all()
    for s in arr_ships:
        days = (s.eta - today).days
        if 0 <= days <= ARRIVAL_LEAD_DAYS:
            ref = s.shipment_ref or f"SH-{s.shipment_id:04d}"
            sev = "HIGH" if days <= 3 else "MEDIUM"
            _add(db, created, existing,
                 key=f"ARRIVAL:{s.shipment_id}:{s.eta}",
                 atype="ARRIVAL_UPCOMING", severity=sev,
                 title=f"Vessel arriving in {days} day(s) — {s.vessel_name or ref}",
                 message=f"Shipment {ref}"
                         f"{' (LC '+s.lc.lc_number+')' if s.lc else ''} is due to arrive on {s.eta} "
                         f"(ETA in {days} day(s)). Prepare clearance and delivery to avoid demurrage.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=s.lc_id, shipment_id=s.shipment_id)

    # ---- 10. FI expiry: deadline to file the GD is approaching / passed ----
    FI_LEAD_DAYS = 7
    fis = db.query(FinancialInstrument).filter(FinancialInstrument.expiry_date.isnot(None)).all()
    for fi in fis:
        # GD considered filed if FI carries a declaration number or its shipment has a GD
        gd_filed = bool(fi.declaration_number) or bool(fi.shipment and fi.shipment.goods_declarations)
        if gd_filed:
            continue
        days = (fi.expiry_date - today).days
        ref = fi.fi_number or f"FI-{fi.fi_id}"
        if days < 0:
            _add(db, created, existing,
                 key=f"FI_EXPIRY_OVERDUE:{fi.fi_id}",
                 atype="FI_EXPIRY", severity="HIGH",
                 title=f"GD filing deadline PASSED — FI {ref}",
                 message=f"The Financial Instrument {ref} expired on {fi.expiry_date} "
                         f"({abs(days)} day(s) ago) and no GD has been filed. File the Goods "
                         f"Declaration immediately to avoid penalties.",
                 entity_type="FI", entity_id=fi.fi_id,
                 lc_id=fi.lc_id, shipment_id=fi.shipment_id)
        elif 0 <= days <= FI_LEAD_DAYS:
            sev = "HIGH" if days <= 2 else "MEDIUM"
            _add(db, created, existing,
                 key=f"FI_EXPIRY:{fi.fi_id}",
                 atype="FI_EXPIRY", severity=sev,
                 title=f"GD filing deadline in {days} day(s) — FI {ref}",
                 message=f"Financial Instrument {ref} expires on {fi.expiry_date} "
                         f"({days} day(s) away) — this is the last date to file the GD. "
                         f"File the Goods Declaration before it lapses.",
                 entity_type="FI", entity_id=fi.fi_id,
                 lc_id=fi.lc_id, shipment_id=fi.shipment_id)

    # ---- 11. Post-last-ship-date doc chase: 2 days after the LC's latest shipment
    #          date, the BL + Invoice + Packing List should be in hand. ----
    POST_SHIP_GRACE_DAYS = 2
    ship_lcs = db.query(LCMaster).filter(
        LCMaster.status.in_(["OPEN", "SHIPPED"]),
        LCMaster.last_ship_date.isnot(None),
    ).all()
    # One query for every shipment across every overdue LC instead of a nested
    # db.query(Shipment)... call per LC (was a query inside the loop).
    overdue_lc_ids = [lc.lc_id for lc in ship_lcs
                      if (today - lc.last_ship_date).days >= POST_SHIP_GRACE_DAYS]
    ships_by_lc = defaultdict(list)
    if overdue_lc_ids:
        for s in db.query(Shipment).options(
            selectinload(Shipment.bill_of_ladings),
            selectinload(Shipment.commercial_invoices),
            selectinload(Shipment.packing_lists),
        ).filter(Shipment.lc_id.in_(overdue_lc_ids), Shipment.is_deleted.is_(False)).all():
            ships_by_lc[s.lc_id].append(s)

    for lc in ship_lcs:
        overdue = (today - lc.last_ship_date).days
        if overdue < POST_SHIP_GRACE_DAYS:
            continue
        ships = ships_by_lc.get(lc.lc_id, [])
        if not ships:
            _add(db, created, existing,
                 key=f"POST_SHIP_DOCS:{lc.lc_id}:none:{lc.last_ship_date}",
                 atype="POST_SHIP_DOCS_MISSING", severity="HIGH",
                 title=f"Shipping docs overdue — LC {lc.lc_number}",
                 message=f"The latest shipment date for LC {lc.lc_number} was {lc.last_ship_date} "
                         f"({overdue} day(s) ago) but no shipment with a BL, Invoice and Packing List "
                         f"has been recorded. Chase the supplier for the shipping documents.",
                 entity_type="LC", entity_id=lc.lc_id, lc_id=lc.lc_id)
            continue
        for s in ships:
            has_bl = len(s.bill_of_ladings) > 0
            has_inv = len(s.commercial_invoices) > 0
            has_pkg = len(s.packing_lists) > 0
            if has_bl and has_inv and has_pkg:
                continue
            missing = [n for n, h in [("BL", has_bl), ("Invoice", has_inv), ("Packing List", has_pkg)] if not h]
            ref = s.shipment_ref or f"SH-{s.shipment_id:04d}"
            _add(db, created, existing,
                 key=f"POST_SHIP_DOCS:{lc.lc_id}:{s.shipment_id}:{'/'.join(missing)}",
                 atype="POST_SHIP_DOCS_MISSING", severity="HIGH",
                 title=f"Shipping docs overdue — LC {lc.lc_number} ({ref})",
                 message=f"The latest shipment date for LC {lc.lc_number} was {lc.last_ship_date} "
                         f"({overdue} day(s) ago) but {', '.join(missing)} "
                         f"{'is' if len(missing) == 1 else 'are'} still missing for {ref}. "
                         f"Obtain the shipping documents.",
                 entity_type="SHIPMENT", entity_id=s.shipment_id,
                 lc_id=lc.lc_id, shipment_id=s.shipment_id)

    # ---- 13. WeBOC GD: filing deadline (ETA + 18/20 days per FBR SRO 1346 from Oct 2026),
    #          late filing (Section 82 penalty), and the into-bond 180-day clock ----
    weboc_gds = db.query(GoodsDeclaration).filter(
        (GoodsDeclaration.eta.isnot(None)) | (GoodsDeclaration.late_filed.is_(True))
    ).all()
    for gd in weboc_gds:
        ref = gd.gd_number or (gd.shipment.shipment_ref if gd.shipment else None) or f"GD-{gd.gd_id}"
        fd = filing_deadline(gd, today)

        # Late filed — Section 82 penalty found on the GD View.
        if fd["late_filed"]:
            pen = float(gd.section82_penalty_pkr or 0)
            est = fd.get("penalty_estimate_pkr")
            if pen > 0:
                pen_txt = f" Section 82 penalty found: PKR {pen:,.0f}."
            elif est:
                pen_txt = (f" Documented Section 82 amount is not yet recorded; "
                           f"FBR SRO 1346 estimate is PKR {est:,.0f} (cap Rs 1,000,000).")
            else:
                pen_txt = " Section 82 penalty found."
            _add(db, created, existing,
                 key=f"GD_LATE_FILED:{gd.gd_id}",
                 atype="GD_LATE_FILED", severity="HIGH",
                 title=f"GD appears to be late filed — {ref}",
                 message=f"GD appears to be late filed.{pen_txt} "
                         f"The extra charge is recorded against this GD for reporting.",
                 entity_type="GD", entity_id=gd.gd_id,
                 lc_id=gd.lc_id, shipment_id=gd.shipment_id)

        # Filing deadline approaching / passed (only while the GD is not yet filed).
        elif fd["state"] == "DUE_SOON":
            days = fd["days_remaining"]
            sev = "HIGH" if days <= 3 else "MEDIUM"
            filing_days = fd.get("filing_days") or GD_FILING_DAYS
            _add(db, created, existing,
                 key=f"GD_FILING_DUE:{gd.gd_id}:{fd['deadline']}",
                 atype="GD_FILING_DUE", severity=sev,
                 title=f"GD filing deadline in {days} day(s) — {ref}",
                 message=f"GD filing deadline is approaching. GD should be filed within "
                         f"{filing_days} days of arrival ({fd['eta']}) — the deadline is "
                         f"{fd['deadline']} ({days} day(s) away). "
                         f"From 1 Oct 2026 FBR SRO 1346 uses 20 days then escalating PKR fines. "
                         f"Upload the GD View once filed.",
                 entity_type="GD", entity_id=gd.gd_id,
                 lc_id=gd.lc_id, shipment_id=gd.shipment_id)
        elif fd["state"] == "OVERDUE":
            late_by = abs(fd["days_remaining"])
            filing_days = fd.get("filing_days") or GD_FILING_DAYS
            est = fd.get("penalty_estimate_pkr")
            est_txt = (f" Estimated SRO 1346 penalty: PKR {est:,.0f} (Rs 25,000/day for 5 days, "
                       f"then Rs 50,000/day, cap Rs 1,000,000)." if est else
                       " A late filing attracts a Section 82 penalty.")
            _add(db, created, existing,
                 key=f"GD_FILING_OVERDUE:{gd.gd_id}:{fd['deadline']}",
                 atype="GD_FILING_OVERDUE", severity="HIGH",
                 title=f"GD filing deadline PASSED — {ref}",
                 message=f"The GD filing deadline ({fd['deadline']} = arrival {fd['eta']} + "
                         f"{filing_days} days) passed {late_by} day(s) ago and no GD View "
                         f"has been uploaded. File the GD.{est_txt}",
                 entity_type="GD", entity_id=gd.gd_id,
                 lc_id=gd.lc_id, shipment_id=gd.shipment_id)

        # Into-bond: over-lift. The 180-day clock and its penalty are handled by
        # services.bond_alerts (below) — those need the daily re-alert / auto-resolve
        # cadence, which the one-shot _add() dedup here cannot express.
        bond = bond_summary(gd, db, today)
        if not bond.get("applies"):
            continue
        rem = bond["remaining_qty_mt"] or 0
        if bond["over_lifted"]:
            _add(db, created, existing,
                 key=f"EX_BOND_EXCEEDED:{gd.gd_id}",
                 atype="EX_BOND_EXCEEDED", severity="HIGH",
                 title=f"Ex-bond quantity exceeds into-bond quantity — {ref}",
                 message=f"Ex-bond liftings total {bond['lifted_qty_mt']:,.3f} MT against an "
                         f"into-bond quantity of {bond['bonded_qty_mt']:,.3f} MT "
                         f"(over by {abs(rem):,.3f} MT). Check the ex-bond entries.",
                 entity_type="GD", entity_id=gd.gd_id,
                 lc_id=gd.lc_id, shipment_id=gd.shipment_id)

    # ---- 14. SRO quota: exceeded / expired with quantity still open ----
    usage = compute_usage(db)
    for a in db.query(EdbApproval).all():
        used = float(usage.get(a.approval_id, {}).get("used_qty_mt") or 0)
        approved = float(a.approved_qty_mt) if a.approved_qty_mt is not None else None
        name = a.main_sro_no or a.approval_no or f"SRO-{a.approval_id}"
        who = a.company_name or "—"
        if approved is not None and used > approved:
            _add(db, created, existing,
                 key=f"SRO_QUOTA_EXCEEDED:{a.approval_id}",
                 atype="SRO_QUOTA_EXCEEDED", severity="HIGH",
                 title=f"SRO quota exceeded — {name} ({who})",
                 message=f"Used {used:,.3f} MT against an approved quota of {approved:,.3f} MT "
                         f"(over by {used-approved:,.3f} MT). Raise the approved quantity or "
                         f"stop drawing on this SRO.",
                 entity_type="SRO", entity_id=a.approval_id)
        elif approved and approved > 0:
            ratio = used / approved
            if ratio >= 0.9:
                _add(db, created, existing,
                     key=f"SRO_QUOTA_90:{a.approval_id}",
                     atype="SRO_QUOTA_90", severity="HIGH",
                     title=f"SRO quota at 90% — {name} ({who})",
                     message=f"Used {used:,.3f} MT of {approved:,.3f} MT approved "
                             f"({ratio*100:.1f}%). Remaining quota is thin — plan remaining GDs.",
                     entity_type="SRO", entity_id=a.approval_id)
            elif ratio >= 0.8:
                _add(db, created, existing,
                     key=f"SRO_QUOTA_80:{a.approval_id}",
                     atype="SRO_QUOTA_80", severity="MEDIUM",
                     title=f"SRO quota at 80% — {name} ({who})",
                     message=f"Used {used:,.3f} MT of {approved:,.3f} MT approved "
                             f"({ratio*100:.1f}%). Watch remaining quota on the SRO report.",
                     entity_type="SRO", entity_id=a.approval_id)
        if a.end_date and a.end_date < today and (approved is None or used < (approved or 0)):
            _add(db, created, existing,
                 key=f"SRO_QUOTA_EXPIRED:{a.approval_id}:{a.end_date}",
                 atype="SRO_QUOTA_EXPIRED", severity="MEDIUM",
                 title=f"SRO quota expired — {name} ({who})",
                 message=f"This SRO quota expired on {a.end_date}. Any remaining quantity can no "
                         f"longer be claimed under it — renew or extend the approval.",
                 entity_type="SRO", entity_id=a.approval_id)

    # ---- 15. SRO usage match unclear (an Item Details item could not be linked) ----
    for u in usage.get("_unclear", []):
        _add(db, created, existing,
             key=f"SRO_MATCH_UNCLEAR:{u['gd_id']}:{u.get('item_number')}",
             atype="SRO_MATCH_UNCLEAR", severity="LOW",
             title=f"SRO match unclear — GD {u.get('gd_number') or u['gd_id']}",
             message=f"Item {u.get('item_number') or ''} (HS {u.get('hs_code') or '—'}, "
                     f"SRO {u.get('sro_no') or '—'}) could not be matched to an SRO quota with "
                     f"confidence, so it is NOT counted as used. {u.get('reason') or ''} "
                     f"Link it manually on the GD if it draws on a quota.",
             entity_type="GD", entity_id=u["gd_id"])

    # ---- 7. Shipment / LC complete (informational) ----
    lcs = db.query(LCMaster).filter(LCMaster.status.in_(["OPEN", "SHIPPED"])).all()
    lc_ids_7 = [lc.lc_id for lc in lcs]
    # Two aggregate queries for the whole batch instead of two per LC (was O(2N) round trips).
    ordered_by_lc = {lc_id: qty for lc_id, qty in (
        db.query(LCProduct.lc_id, func.coalesce(func.sum(LCProduct.quantity), 0))
          .filter(LCProduct.lc_id.in_(lc_ids_7)).group_by(LCProduct.lc_id).all()
    )} if lc_ids_7 else {}
    delivered_by_lc = {lc_id: qty for lc_id, qty in (
        db.query(Shipment.lc_id, func.coalesce(func.sum(Shipment.delivered_quantity_mt), 0))
          .filter(Shipment.lc_id.in_(lc_ids_7), Shipment.is_deleted.is_(False))
          .group_by(Shipment.lc_id).all()
    )} if lc_ids_7 else {}
    for lc in lcs:
        ordered = float(ordered_by_lc.get(lc.lc_id, 0) or 0)
        delivered = float(delivered_by_lc.get(lc.lc_id, 0) or 0)
        if ordered > 0 and delivered >= ordered - VARIANCE_TOLERANCE_MT:
            _add(db, created, existing,
                 key=f"SHIPMENT_COMPLETE:{lc.lc_id}",
                 atype="SHIPMENT_COMPLETE", severity="LOW",
                 title=f"LC {lc.lc_number} fully delivered",
                 message=f"All {ordered:.3f} MT ordered under LC {lc.lc_number} have been delivered "
                         f"({delivered:.3f} MT). Ready for close-out.",
                 entity_type="LC", entity_id=lc.lc_id, lc_id=lc.lc_id)

    # ---- 15b. Per-container last free day (manual milestones) ----
    lfd_events = (db.query(ContainerEvent)
                    .filter(ContainerEvent.last_free_date.isnot(None))
                    .all())
    for ev in lfd_events:
        days = (ev.last_free_date - today).days
        if days > 3:
            continue
        ref = ev.container_number
        sev = "HIGH" if days <= 0 else "MEDIUM"
        atype = "CONTAINER_LFD"
        title = (f"Container last free day PASSED — {ref}" if days < 0
                 else f"Container last free day in {days} day(s) — {ref}")
        _add(db, created, existing,
             key=f"CONTAINER_LFD:{ev.event_id}:{ev.last_free_date}",
             atype=atype, severity=sev,
             title=title,
             message=f"Last free day for container {ref} is {ev.last_free_date} "
                     f"({ev.event_type.replace('_', ' ').title()}). "
                     f"Gate-out before demurrage/detention accrues.",
             entity_type="SHIPMENT", entity_id=ev.shipment_id,
             shipment_id=ev.shipment_id)

    # ---- 16. Into-bond 180-day clock: day-160 reminder, deadline passed, penalty due.
    #          Recurring (daily re-alert after acknowledge) and self-resolving, so it
    #          lives in its own module rather than the one-shot rules above. ----
    bond_alerts = {"created": 0, "resolved": 0, "penalties_voided": 0}
    if settings.BOND_ALERTS_ENABLED:
        bond_alerts = scan_bond_alerts(db)

    db.commit()

    summary = {}
    for t in created:
        summary[t] = summary.get(t, 0) + 1
    total = len(created) + bond_alerts["created"]
    logger.info(f"Alert scan complete: {total} new alerts {summary} bond={bond_alerts}")
    return {"new_alerts": total, "by_type": summary, "bond_alerts": bond_alerts}
