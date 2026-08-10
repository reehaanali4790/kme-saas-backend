"""
Report Master — template CRUD and unified query engine.

Fetches full rows from existing report builders, applies filters, projects
selected columns, and sorts. Does not replace the built-in report pages.

Ported from rehan/newchanges (backend/services/report_master_service.py).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload, selectinload

from models.database_models import (
    ReportTemplate, Shipment, LCMaster, GoodsDeclaration, BillOfLading, DemurrageConfig, InsuranceCertificate,
    Contract,
)
from modules.reports.report_field_catalog import (
    field_label, GRAINS, MASTER_GRAIN, qualify_key, normalize_columns,
)
from modules.weboc.gd_service import examination_report_available
from infrastructure.normalization.normalization_service import (
    company_resolver, enrich_company_fields,
)
from modules.shipments.demurrage_service import compute_demurrage, demurrage_report
from modules.shipments.container_detention_service import (
    compute_container_detention, build_container_detention_report,
)
from modules.weboc.helpers.gd_balance_report import gd_balance_report, gd_balance_filter_options
from modules.reports.service import pending_order_report
from modules.weboc.helpers.weboc_service import bond_summary


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes")


def _match_any(value, wanted) -> bool:
    """True if `value` equals any entry of `wanted` (case-insensitive). `wanted` may be
    a single string (legacy single-select filters / old saved templates) or a list
    (multi-select). An empty/missing `wanted` matches everything — callers still guard
    with `if filters.get(key):` before calling this."""
    opts = wanted if isinstance(wanted, (list, tuple, set)) else [wanted]
    opts = {str(o).strip().upper() for o in opts if o not in (None, "")}
    if not opts:
        return True
    return str(value or "").strip().upper() in opts


def _delegate_single(v):
    """Value to pass to a single-value-only delegate function (demurrage_report /
    build_container_detention_report). A plain string narrows their DB-level query as
    before; a multi-select list can't be expressed as one value, so we pass None (no
    narrowing there) and let the post-fetch _match_any() do the OR-match in Python."""
    if isinstance(v, (list, tuple, set)):
        return None
    return v


def _call_demurrage_report(db: Session, filters: dict) -> dict:
    """Adapter — our demurrage_report() takes individual kwargs (with real `date`
    objects), while the rest of Report Master passes a flat string-keyed filter dict."""
    return demurrage_report(
        db,
        state=_delegate_single(filters.get("state")),
        date_from=_parse_date(filters.get("date_from")),
        date_to=_parse_date(filters.get("date_to")),
        vessel=_delegate_single(filters.get("vessel")),
        port=_delegate_single(filters.get("port")),
        importer=_delegate_single(filters.get("importer")),
        q=filters.get("q"),
        open_only=_truthy(filters.get("open_only")),
    )


def _get_path(row: dict, key: str):
    if "." in key:
        cur = row
        for part in key.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur
    return row.get(key)


def _project_row(row: dict, columns: list[str]) -> dict:
    return {k: _get_path(row, k) for k in columns}


def _sort_rows(rows: list[dict], sort_field: Optional[str], sort_dir: str) -> list[dict]:
    if not sort_field or not rows:
        return rows
    reverse = (sort_dir or "asc").lower() == "desc"

    def _key(r):
        v = _get_path(r, sort_field)
        if v is None:
            return (1, "")
        if isinstance(v, (int, float)):
            return (0, v)
        return (0, str(v).lower())

    return sorted(rows, key=_key, reverse=reverse)


def template_to_dict(t: ReportTemplate) -> dict:
    return {
        "template_id": t.template_id,
        "name": t.name,
        "description": t.description,
        "grain": t.grain,
        "columns": t.columns or [],
        "filters": t.filters or {},
        "sort_field": t.sort_field,
        "sort_dir": t.sort_dir or "asc",
        "sidebar_order": t.sidebar_order or 0,
        "is_builtin": bool(t.is_builtin),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def list_templates(db: Session, user_id: int) -> list[dict]:
    rows = (
        db.query(ReportTemplate)
        .filter(ReportTemplate.user_id == user_id)
        .order_by(ReportTemplate.sidebar_order, ReportTemplate.name)
        .all()
    )
    return [template_to_dict(t) for t in rows]


def get_template(db: Session, template_id: int, user_id: int) -> Optional[ReportTemplate]:
    return (
        db.query(ReportTemplate)
        .filter(ReportTemplate.template_id == template_id, ReportTemplate.user_id == user_id)
        .first()
    )


def create_template(db: Session, user_id: int, data: dict) -> ReportTemplate:
    grain = (data.get("grain") or MASTER_GRAIN).lower()
    cols = data.get("columns") or []
    grain, cols = normalize_columns(grain, cols)
    if grain not in GRAINS:
        raise ValueError(f"Invalid grain: {grain}")
    if not cols:
        raise ValueError("At least one valid column is required")

    max_order = (
        db.query(ReportTemplate.sidebar_order)
        .filter(ReportTemplate.user_id == user_id)
        .order_by(ReportTemplate.sidebar_order.desc())
        .first()
    )
    next_order = (max_order[0] + 1) if max_order and max_order[0] is not None else 0

    t = ReportTemplate(
        user_id=user_id,
        name=(data.get("name") or "Untitled Report").strip()[:120],
        description=(data.get("description") or "").strip() or None,
        grain=grain,
        columns=cols,
        filters=data.get("filters") or {},
        sort_field=data.get("sort_field"),
        sort_dir=(data.get("sort_dir") or "asc")[:4],
        sidebar_order=data.get("sidebar_order", next_order),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def update_template(db: Session, t: ReportTemplate, data: dict) -> ReportTemplate:
    if "name" in data and data["name"]:
        t.name = str(data["name"]).strip()[:120]
    if "description" in data:
        t.description = (data["description"] or "").strip() or None
    if "columns" in data:
        grain, cols = normalize_columns(t.grain, data["columns"] or [])
        if not cols:
            raise ValueError("At least one valid column is required")
        t.grain = grain
        t.columns = cols
    if "filters" in data:
        t.filters = data["filters"] or {}
    if "sort_field" in data:
        t.sort_field = data["sort_field"]
    if "sort_dir" in data:
        t.sort_dir = (data["sort_dir"] or "asc")[:4]
    if "sidebar_order" in data:
        t.sidebar_order = int(data["sidebar_order"])
    t.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(t)
    return t


def delete_template(db: Session, t: ReportTemplate) -> None:
    db.delete(t)
    db.commit()


def _primary_bl(shipment: Shipment) -> BillOfLading | None:
    bls = sorted(shipment.bill_of_ladings or [], key=lambda b: b.bl_id or 0)
    return bls[0] if bls else None


def _primary_gd(shipment: Shipment) -> GoodsDeclaration | None:
    gds = sorted(shipment.goods_declarations or [], key=lambda g: g.gd_id or 0)
    return gds[-1] if gds else None


def _primary_insurance(shipment: Shipment) -> InsuranceCertificate | None:
    incs = sorted(shipment.insurance_certificates or [], key=lambda i: i.insurance_id or 0)
    return incs[-1] if incs else None


def _insurance_snapshot(ins: InsuranceCertificate | None) -> dict:
    if not ins:
        return {}
    gp = float(ins.gross_premium) if ins.gross_premium is not None else (float(ins.net_premium) if ins.net_premium is not None else None)
    si = float(ins.sum_insured) if (ins.sum_insured is not None and float(ins.sum_insured) > 0) else None
    rate = round((gp / si) * 100, 4) if (gp is not None and si is not None) else None
    return {
        "certificate_number": ins.certificate_number,
        "policy_number": ins.policy_number,
        "insurance_company": ins.insurance_company,
        "issue_date": ins.issue_date.isoformat() if ins.issue_date else None,
        "sum_insured": float(ins.sum_insured) if ins.sum_insured is not None else None,
        "currency": ins.currency,
        "gross_premium": float(ins.gross_premium) if ins.gross_premium is not None else None,
        "net_premium": float(ins.net_premium) if ins.net_premium is not None else None,
        "premium_rate_pct": rate,
        "insurance_status": ins.status,
    }


def _buyer_label(lc: LCMaster | None) -> str | None:
    if not lc:
        return None
    if lc.booked_by and lc.booked_by.strip():
        return lc.booked_by.strip()
    names = [a.buyer_name.strip() for a in (lc.buyer_allocations or []) if a.buyer_name]
    if not names:
        return None
    return "+".join(names) if len(names) > 1 else names[0]


def _bl_snapshot(bl: BillOfLading, cfg) -> dict:
    dem = compute_demurrage(bl, cfg)
    container = bl.bl_type == "CONTAINER"
    det = compute_container_detention(bl, cfg) if container else {}
    return {
        "bl_number": bl.bl_number,
        "port_of_discharge": bl.port_of_discharge,
        "bl_date": bl.bl_date.isoformat() if bl.bl_date else None,
        "gross_weight_mt": float(bl.gross_weight_mt) if bl.gross_weight_mt is not None else None,
        "package_type": bl.package_type,
        "is_container": container,
        "demurrage_state": dem.get("state"),
        "demurrage_start_date": dem.get("start_date"),
        "demurrage_last_free_day": dem.get("last_free_day"),
        "demurrage_days_remaining": dem.get("days_remaining"),
        "demurrage_chargeable_days": dem.get("chargeable_days"),
        "demurrage_total_amount": dem.get("total_amount"),
        "demurrage_cleared_date": dem.get("cleared_date"),
        "detention_state": det.get("state") if det else None,
        "detention_start_date": det.get("start_date") if det else None,
        "detention_last_free_day": det.get("last_free_day") if det else None,
        "detention_chargeable_days": det.get("chargeable_days") if det else None,
        "detention_total_amount": det.get("total_amount") if det else None,
        "detention_end_date": det.get("end_date") if det else None,
    }


def _gd_snapshot(gd: GoodsDeclaration, db: Session, lc: LCMaster | None, today: date) -> dict:
    bond = bond_summary(gd, db, today)
    prod = lc.products[0] if (lc and lc.products) else None
    filing = bond.get("ib_start_date")
    if not filing and gd.filing_date:
        filing = gd.filing_date.isoformat()
    paid = bond.get("duties_paid_pkr")
    if paid is None and gd.total_duties_pkr is not None:
        paid = float(gd.total_duties_pkr)
    return {
        "gd_number": gd.gd_number,
        "gd_type": gd.gd_type,
        "gd_status": gd.status,
        "is_verified": bool(getattr(gd, "is_verified", False)),
        "declaration_type": (gd.declaration_type_raw or "").strip() or None,
        "item": (prod.item_code or prod.product_code) if prod else None,
        "origin": (prod.origin if prod else None) or gd.country_of_origin,
        "ib_gd_date": filing,
        "ib_expiry_date": bond.get("deadline"),
        "remaining_days": bond.get("days_remaining"),
        "balance_ex_bond_kg": (
            float(bond["remaining_qty_mt"]) * 1000
            if bond.get("remaining_qty_mt") is not None else None
        ),
        "bond_state": bond.get("state"),
        "is_settled": bool(bond.get("is_weight_settled")),
        "penalty_pkr": bond.get("penalty_pkr"),
        "duties_paid_pkr": paid,
        "potential_duty_pkr": bond.get("potential_duty_pkr"),
        "sro_applicable": None,
        "late_filing": bool(gd.late_filed),
        "examination_report_available": examination_report_available(gd, db),
        "delivery_status": gd.status if (gd.status or "").upper() in ("RELEASED", "CLEARED") else "PENDING",
    }


def _lc_snapshot(lc: LCMaster | None) -> dict:
    if not lc:
        return {}
    prod = lc.products[0] if lc.products else None
    if not prod:
        return {"lc_number": lc.lc_number, "lc_status": lc.status}
    qty = float(prod.quantity) if prod.quantity is not None else None
    rate = float(prod.lc_unit_price) if prod.lc_unit_price is not None else None
    amount = float(prod.lc_amount) if prod.lc_amount is not None else (
        round(qty * rate, 2) if qty is not None and rate is not None else None
    )
    return {
        "lc_number": lc.lc_number,
        "item": (prod.item_code or prod.product_code or prod.product_name or "").strip() or None,
        "size": (prod.size or "").strip() or None,
        "grade": (prod.grade or "").strip() or None,
        "mill_name": (prod.mill_name or "").strip() or None,
        "buyer": _buyer_label(lc),
        "supplier": (lc.supplier_name or "").strip() or None,
        "origin": (prod.origin or "").strip() or None,
        "order_qty_mt": round(qty, 3) if qty is not None else None,
        "lc_rate": rate,
        "lc_line_amount": amount,
        "currency": (lc.currency or "USD").strip(),
        "lc_date": lc.lc_date.isoformat() if lc.lc_date else None,
        "last_ship_date": lc.last_ship_date.isoformat() if lc.last_ship_date else None,
        "lc_status": lc.status,
        "payment_terms": lc.payment_terms,
    }


def _build_master_row(
    shipment: Shipment,
    db: Session,
    cfg,
    today: date,
) -> dict:
    from modules.shipments.services import shipment_summary

    row: dict[str, Any] = {}
    summary = shipment_summary(shipment)
    summary["bl_count"] = len(shipment.bill_of_ladings or [])
    summary["gd_count"] = len(shipment.goods_declarations or [])

    for k, v in summary.items():
        row[qualify_key("shipment", k)] = v

    bl = _primary_bl(shipment)
    if bl:
        for k, v in _bl_snapshot(bl, cfg).items():
            row[qualify_key("bl", k)] = v

    gd = _primary_gd(shipment)
    lc = shipment.lc
    if gd:
        for k, v in _gd_snapshot(gd, db, lc, today).items():
            row[qualify_key("gd", k)] = v

    for k, v in _lc_snapshot(lc).items():
        row[qualify_key("lc", k)] = v

    ins = _primary_insurance(shipment)
    if ins:
        for k, v in _insurance_snapshot(ins).items():
            row[qualify_key("insurance", k)] = v

    contract = lc.contract if lc else None
    if contract:
        for k, v in _contract_snapshot(contract).items():
            row[qualify_key("contract", k)] = v

    return row


def _fetch_master_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    today = date.today()
    cfg = db.query(DemurrageConfig).order_by(DemurrageConfig.config_id).first()

    q = (
        db.query(Shipment)
        .filter(Shipment.is_deleted.is_(False))
        .options(
            joinedload(Shipment.lc).options(
                joinedload(LCMaster.products),
                joinedload(LCMaster.buyer_allocations),
                joinedload(LCMaster.contract).selectinload(Contract.items),
            ),
            joinedload(Shipment.bill_of_ladings),
            joinedload(Shipment.commercial_invoices),
            joinedload(Shipment.packing_lists),
            joinedload(Shipment.goods_declarations),
            joinedload(Shipment.financial_instruments),
            joinedload(Shipment.extra_documents),
        )
    )
    base_shipments = q.order_by(Shipment.created_at.desc()).all()
    cr = company_resolver(db)
    all_unfiltered_rows = [_build_master_row(s, db, cfg, today) for s in base_shipments]
    for r in all_unfiltered_rows:
        imp = r.get(qualify_key("shipment", "importer_name"))
        if imp:
            res = cr.resolve(imp)
            r[qualify_key("shipment", "company_code")] = res.get("short_code")

    def _party_name(r):
        return (r.get(qualify_key("shipment", "party_name"))
                or r.get(qualify_key("shipment", "booked_by")) or "").strip() or None

    options = {
        "statuses": sorted({str(r.get(qualify_key("shipment", "status"))) for r in all_unfiltered_rows if r.get(qualify_key("shipment", "status"))}),
        "vessels": sorted({str(r.get(qualify_key("shipment", "vessel_name"))) for r in all_unfiltered_rows if r.get(qualify_key("shipment", "vessel_name"))}),
        "importers": sorted({str(r.get(qualify_key("shipment", "company_code"))) for r in all_unfiltered_rows if r.get(qualify_key("shipment", "company_code"))}),
        "banks": sorted({str(r.get(qualify_key("shipment", "bank_name"))) for r in all_unfiltered_rows if r.get(qualify_key("shipment", "bank_name"))}),
        "item_types": sorted({str(r.get(qualify_key("shipment", "item_category"))) for r in all_unfiltered_rows if r.get(qualify_key("shipment", "item_category"))}),
        "parties": sorted({str(_party_name(r)) for r in all_unfiltered_rows if _party_name(r)}),
        "gd_types": ["HOME_CONSUMPTION", "INTO_BOND", "EX_BOND"],
        "gd_statuses": sorted({str(r.get(qualify_key("gd", "gd_status"))) for r in all_unfiltered_rows if r.get(qualify_key("gd", "gd_status"))}),
        "demurrage_states": ["UNKNOWN", "OK", "AT_RISK", "ACCRUING", "CLEARED"],
        "bond_states": sorted({str(r.get(qualify_key("gd", "bond_state"))) for r in all_unfiltered_rows if r.get(qualify_key("gd", "bond_state"))}),
    }

    rows = all_unfiltered_rows
    if filters.get("status"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("shipment", "status")), filters["status"])]
    if filters.get("vessel"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("shipment", "vessel_name")), filters["vessel"])]
    if filters.get("importer"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("shipment", "company_code")), filters["importer"])]
    if filters.get("item_type"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("shipment", "item_category")), filters["item_type"])]
    if filters.get("party"):
        rows = [r for r in rows if _match_any(_party_name(r), filters["party"])]
    if filters.get("bank"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("shipment", "bank_name")), filters["bank"])]
    if filters.get("search"):
        needle = str(filters["search"]).strip().lower()
        keys = [
            qualify_key("shipment", k) for k in (
                "lc_number", "vessel_name", "party_name", "company_code", "importer_name",
                "booked_by", "hoa", "shipment_ref", "lot_number", "bank_name", "item_category",
            )
        ] + [qualify_key("gd", "gd_number"), qualify_key("bl", "bl_number")]
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in keys).lower()]

    ef, et = _parse_date(filters.get("eta_from")), _parse_date(filters.get("eta_to"))
    if ef or et:
        kept = []
        for r in rows:
            raw = r.get(qualify_key("shipment", "eta"))
            if not raw:
                continue
            dd = date.fromisoformat(str(raw)[:10])
            if ef and dd < ef:
                continue
            if et and dd > et:
                continue
            kept.append(r)
        rows = kept

    if filters.get("gd_type"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("gd", "gd_type")), filters["gd_type"])]
    if filters.get("gd_status"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("gd", "gd_status")), filters["gd_status"])]
    if filters.get("demurrage_state"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("bl", "demurrage_state")), filters["demurrage_state"])]
    if filters.get("bond_state"):
        rows = [r for r in rows if _match_any(r.get(qualify_key("gd", "bond_state")), filters["bond_state"])]
    if _truthy(filters.get("has_gd", "")):
        rows = [r for r in rows if (r.get(qualify_key("shipment", "gd_count")) or 0) > 0]
    if _truthy(filters.get("has_bl", "")):
        rows = [r for r in rows if (r.get(qualify_key("shipment", "bl_count")) or 0) > 0]

    return rows, options


def _fetch_shipment_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    from modules.shipments.services import shipment_summary

    q = (
        db.query(Shipment)
        .filter(Shipment.is_deleted.is_(False))
        .options(
            joinedload(Shipment.lc).options(
                joinedload(LCMaster.products),
                joinedload(LCMaster.buyer_allocations),
            ),
            joinedload(Shipment.bill_of_ladings),
            joinedload(Shipment.commercial_invoices),
            joinedload(Shipment.packing_lists),
            joinedload(Shipment.goods_declarations).joinedload(GoodsDeclaration.kgtl_weighments),
            joinedload(Shipment.financial_instruments),
            joinedload(Shipment.extra_documents),
        )
    )
    base_shipments = q.order_by(Shipment.created_at.desc()).all()
    cr = company_resolver(db)
    all_unfiltered_rows = [shipment_summary(s) for s in base_shipments]
    for r in all_unfiltered_rows:
        enrich_company_fields(r, cr, field="importer_name")

    def _party_name(r):
        return (r.get("party_name") or r.get("booked_by") or "").strip() or None

    options = {
        "statuses": sorted({str(r["status"]) for r in all_unfiltered_rows if r.get("status")}),
        "vessels": sorted({str(r["vessel_name"]) for r in all_unfiltered_rows if r.get("vessel_name")}),
        "importers": sorted({str(r["company_code"]) for r in all_unfiltered_rows if r.get("company_code")}),
        "banks": sorted({str(r["bank_name"]) for r in all_unfiltered_rows if r.get("bank_name")}),
        "item_types": sorted({str(r["item_category"]) for r in all_unfiltered_rows if r.get("item_category")}),
        "parties": sorted({str(_party_name(r)) for r in all_unfiltered_rows if _party_name(r)}),
    }

    rows = all_unfiltered_rows
    if filters.get("status"):
        rows = [r for r in rows if _match_any(r.get("status"), filters["status"])]
    if filters.get("vessel"):
        rows = [r for r in rows if _match_any(r.get("vessel_name"), filters["vessel"])]
    if filters.get("importer"):
        rows = [r for r in rows if _match_any(r.get("company_code"), filters["importer"])]
    if filters.get("item_type"):
        rows = [r for r in rows if _match_any(r.get("item_category"), filters["item_type"])]
    if filters.get("party"):
        rows = [r for r in rows if _match_any(_party_name(r), filters["party"])]
    if filters.get("bank"):
        rows = [r for r in rows if _match_any(r.get("bank_name"), filters["bank"])]
    if filters.get("search"):
        needle = str(filters["search"]).strip().lower()
        rows = [
            r for r in rows
            if needle in " ".join(str(r.get(k) or "") for k in (
                "lc_number", "vessel_name", "party_name", "company_code", "importer_name",
                "booked_by", "hoa", "shipment_ref", "lot_number", "bank_name", "item_category",
            )).lower()
        ]
    ef, et = _parse_date(filters.get("eta_from")), _parse_date(filters.get("eta_to"))
    if ef or et:
        kept = []
        for r in rows:
            raw = r.get("eta")
            if not raw:
                continue
            dd = date.fromisoformat(raw)
            if ef and dd < ef:
                continue
            if et and dd > et:
                continue
            kept.append(r)
        rows = kept

    return rows, options


def _fetch_bl_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    kind = (filters.get("bl_kind") or "all").lower()
    rows_out: list[dict] = []
    options: dict = {}

    # state/vessel/port/importer may be multi-select lists — the demurrage/detention
    # delegates below only take one value each, so a list is left out of the delegate
    # call (no DB-level narrowing on that field) and matched via _match_any() after.
    delegate_filters = {
        k: (_delegate_single(v) if k in ("state", "vessel", "port", "importer") else v)
        for k, v in filters.items() if k != "bl_kind"
    }

    if kind in ("all", "coil", "demurrage"):
        dem = _call_demurrage_report(db, delegate_filters)
        options = dem.get("options") or {}
        for r in dem.get("rows") or []:
            dem_block = r.get("demurrage") or {}
            rows_out.append({
                "bl_id": r.get("bl_id"),
                "bl_number": r.get("bl_number"),
                "lc_number": r.get("lc_number"),
                "company_code": r.get("company_code"),
                "importer_name": r.get("importer_name"),
                "vessel_name": r.get("vessel_name"),
                "port_of_discharge": r.get("port_of_discharge"),
                "bl_date": r.get("bl_date"),
                "gross_weight_mt": r.get("gross_weight_mt"),
                "package_type": None,
                "is_container": False,
                "demurrage_state": dem_block.get("state"),
                "demurrage_start_date": dem_block.get("start_date"),
                "demurrage_last_free_day": dem_block.get("last_free_day"),
                "demurrage_days_remaining": dem_block.get("days_remaining"),
                "demurrage_chargeable_days": dem_block.get("chargeable_days"),
                "demurrage_total_amount": dem_block.get("total_amount"),
                "demurrage_cleared_date": dem_block.get("cleared_date"),
                "detention_state": None,
                "detention_start_date": None,
                "detention_last_free_day": None,
                "detention_chargeable_days": None,
                "detention_total_amount": None,
                "detention_end_date": None,
            })

    if kind in ("all", "container", "detention"):
        det = build_container_detention_report(db, delegate_filters)
        if not options:
            options = det.get("options") or {}
        options["bl_kinds"] = ["all", "coil", "container", "demurrage", "detention"]
        for r in det.get("rows") or []:
            det_block = r.get("detention") or {}
            rows_out.append({
                "bl_id": r.get("bl_id"),
                "bl_number": r.get("bl_number"),
                "lc_number": r.get("lc_number"),
                "company_code": r.get("company_code"),
                "importer_name": r.get("importer_name"),
                "vessel_name": r.get("vessel_name"),
                "port_of_discharge": r.get("port_of_discharge"),
                "bl_date": None,
                "gross_weight_mt": None,
                "package_type": r.get("package_type"),
                "is_container": True,
                "demurrage_state": None,
                "demurrage_start_date": None,
                "demurrage_last_free_day": None,
                "demurrage_days_remaining": None,
                "demurrage_chargeable_days": None,
                "demurrage_total_amount": None,
                "demurrage_cleared_date": None,
                "detention_state": det_block.get("state"),
                "detention_start_date": det_block.get("start_date"),
                "detention_last_free_day": det_block.get("last_free_day"),
                "detention_chargeable_days": det_block.get("chargeable_days"),
                "detention_total_amount": det_block.get("total_amount"),
                "detention_end_date": det_block.get("end_date"),
            })
    else:
        options.setdefault("bl_kinds", ["all", "coil", "container", "demurrage", "detention"])

    # Post-fetch OR-match — covers multi-select (the delegates above only narrowed on a
    # single value, if any) and is a harmless no-op re-check for the single-value case.
    # A BL row only ever has demurrage_state OR detention_state populated, never both.
    if filters.get("state"):
        rows_out = [r for r in rows_out if _match_any(r.get("demurrage_state") or r.get("detention_state"), filters["state"])]
    if filters.get("vessel"):
        rows_out = [r for r in rows_out if _match_any(r.get("vessel_name"), filters["vessel"])]
    if filters.get("port"):
        rows_out = [r for r in rows_out if _match_any(r.get("port_of_discharge"), filters["port"])]
    if filters.get("importer"):
        rows_out = [r for r in rows_out if _match_any(r.get("company_code"), filters["importer"])]

    return rows_out, options


def _fetch_gd_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    # gd_type picks the report *mode* (which table/join gd_balance_report runs), not a
    # simple row filter, so it stays single-valued even though the UI's other selects
    # are multi — a list here just takes the first choice rather than erroring.
    gd_type = (_delegate_single(filters.get("gd_type")) or "INTO_BOND").upper()
    include_settled = _truthy(filters.get("include_settled", "true")) if filters.get("include_settled") is not None else True
    result = gd_balance_report(
        db,
        date_from=_parse_date(filters.get("date_from")),
        date_to=_parse_date(filters.get("date_to")),
        vessel=filters.get("vessel"),
        company=filters.get("company"),
        bank=filters.get("bank"),
        gd_status=filters.get("gd_status"),
        include_settled=include_settled,
        gd_type=gd_type,
    )
    opts = gd_balance_filter_options(db, gd_type=gd_type)
    opts["gd_types"] = ["HOME_CONSUMPTION", "INTO_BOND", "EX_BOND"]
    rows = []
    for r in result.get("rows") or []:
        rows.append({
            "gd_id": r.get("gd_id"),
            "gd_number": r.get("ib_gd_number") or r.get("gd_number"),
            "lc_reference": r.get("lc_reference"),
            "shipment_ref": r.get("shipment_ref"),
            "gd_type": r.get("gd_type"),
            "gd_status": r.get("gd_status"),
            "declaration_type": r.get("declaration_type"),
            "importer": r.get("importer"),
            "company_code": r.get("company_code"),
            "buyer": r.get("buyer"),
            "bank_name": r.get("bank_name"),
            "vessel_name": r.get("vessel_name"),
            "item": r.get("item"),
            "origin": r.get("origin"),
            "ib_gd_number": r.get("ib_gd_number"),
            "ib_gd_date": r.get("ib_gd_date"),
            "ib_expiry_date": r.get("ib_expiry_date"),
            "remaining_days": r.get("remaining_days"),
            "balance_ex_bond_kg": r.get("balance_ex_bond_kg"),
            "bond_state": r.get("bond_state"),
            "is_settled": r.get("is_settled"),
            "penalty_pkr": r.get("penalty_pkr"),
            "duties_paid_pkr": r.get("duties_paid_pkr"),
            "potential_duty_pkr": r.get("potential_duty_pkr"),
            "sro_applicable": r.get("sro_applicable"),
            "late_filing": r.get("late_filing"),
            "delivery_status": r.get("delivery_status"),
            "report_mode": r.get("report_mode"),
        })
    return rows, opts


def _fetch_lc_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    result = pending_order_report(
        db,
        buyer=filters.get("buyer"),
        item=filters.get("item"),
        origin=filters.get("origin"),
        importer=filters.get("importer"),
        bank=filters.get("bank"),
        search=filters.get("search"),
        eta_from=_parse_date(filters.get("eta_from")),
        eta_to=_parse_date(filters.get("eta_to")),
    )
    return result.get("rows") or [], result.get("options") or {}


def _contract_row(c: Contract) -> dict:
    """Flat report row for one Purchase Contract — one row per contract (not per line
    item). Line-item columns show the first line + aggregate totals across all lines,
    mirroring how _lc_snapshot() represents an LC's multi-product line as one row."""
    items = sorted(c.items or [], key=lambda x: x.line_no or 0)
    first = items[0] if items else None
    sent = c.sent_to_bank_at
    lc_received = c.lc.import_date if c.lc else None
    days_to_lc = (lc_received.date() - sent.date()).days if (sent and lc_received) else None
    return {
        "contract_id": c.contract_id,
        "contract_number": c.contract_number,
        "status": c.status,
        "source": c.source,
        "contract_date": c.contract_date.isoformat() if c.contract_date else None,
        "valid_to": c.valid_to.isoformat() if c.valid_to else None,
        "sent_to_bank_at": sent.isoformat() if sent else None,
        "lc_received_at": lc_received.isoformat() if lc_received else None,
        "days_to_lc": days_to_lc,
        "supplier_name": c.supplier_name,
        "buyer_name": c.buyer_name,
        "indentor_name": c.indentor_name,
        "buyer_ntn": c.buyer_ntn,
        "buyer_address": c.buyer_address,
        "supplier_address": c.supplier_address,
        "bank_name": c.bank_name,
        "lc_id": c.lc.lc_id if c.lc else None,
        "lc_number": c.lc.lc_number if c.lc else None,
        "lc_status": c.lc.status if c.lc else None,
        "payment_terms": c.payment_terms,
        "currency": c.currency,
        "delivery_terms": c.delivery_terms,
        "country_of_origin": c.country_of_origin,
        "quantity_tolerance": c.quantity_tolerance,
        "port_of_loading": c.port_of_loading,
        "shipping_mark": c.shipping_mark,
        "hs_code": c.hs_code,
        "beneficiary_bank": c.beneficiary_bank,
        "beneficiary_swift": c.beneficiary_swift,
        "beneficiary_account": c.beneficiary_account,
        "beneficiary_bank_addr": c.beneficiary_bank_addr,
        "documents_required": c.documents_required,
        "notes": c.notes,
        "item_count": len(items),
        "product_name": first.product_name if first else None,
        "size_description": first.size_description if first else None,
        "grade_description": first.grade_description if first else None,
        "mill_name": first.mill_name if first else None,
        "total_weight_mt": float(sum((it.weight_mt or 0) for it in items)) if items else None,
        "total_lc_amount": float(sum((it.lc_amount or 0) for it in items)) if items else None,
        "total_purchase_amount": float(sum((it.purchase_amount or 0) for it in items)) if items else None,
    }


def _contract_snapshot(c: Contract | None) -> dict:
    """Same shape as _contract_row(), minus contract_id, for merging into a master
    (per-shipment) row via shipment.lc.contract — mirrors _lc_snapshot()/_gd_snapshot()."""
    if not c:
        return {}
    row = _contract_row(c)
    row.pop("contract_id", None)
    return row


def _fetch_contract_rows(db: Session, filters: dict) -> tuple[list[dict], dict]:
    q = db.query(Contract).options(joinedload(Contract.lc), selectinload(Contract.items))
    all_rows = [_contract_row(c) for c in q.order_by(Contract.contract_id.desc()).all()]

    options = {
        "statuses": sorted({r["status"] for r in all_rows if r.get("status")}),
        "banks": sorted({r["bank_name"] for r in all_rows if r.get("bank_name")}),
        "suppliers": sorted({r["supplier_name"] for r in all_rows if r.get("supplier_name")}),
    }

    rows = all_rows
    if filters.get("status"):
        rows = [r for r in rows if _match_any(r.get("status"), filters["status"])]
    if filters.get("bank"):
        rows = [r for r in rows if _match_any(r.get("bank_name"), filters["bank"])]
    if filters.get("supplier"):
        rows = [r for r in rows if _match_any(r.get("supplier_name"), filters["supplier"])]
    if filters.get("search"):
        needle = str(filters["search"]).strip().lower()
        keys = ("contract_number", "supplier_name", "buyer_name", "lc_number", "bank_name")
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in keys).lower()]
    if _truthy(filters.get("has_lc", "")):
        rows = [r for r in rows if r.get("lc_id") is not None]

    ef, et = _parse_date(filters.get("date_from")), _parse_date(filters.get("date_to"))
    if ef or et:
        kept = []
        for r in rows:
            raw = r.get("contract_date")
            if not raw:
                continue
            dd = date.fromisoformat(str(raw)[:10])
            if ef and dd < ef:
                continue
            if et and dd > et:
                continue
            kept.append(r)
        rows = kept

    return rows, options


def fetch_rows(db: Session, grain: str, filters: dict | None = None) -> tuple[list[dict], dict]:
    f = filters or {}
    g = grain.lower()
    if g == MASTER_GRAIN:
        return _fetch_master_rows(db, f)
    if g == "shipment":
        return _fetch_shipment_rows(db, f)
    if g == "bl":
        return _fetch_bl_rows(db, f)
    if g == "gd":
        return _fetch_gd_rows(db, f)
    if g == "lc":
        return _fetch_lc_rows(db, f)
    if g == "contract":
        return _fetch_contract_rows(db, f)
    raise ValueError(f"Unknown grain: {grain}")


def execute_query(
    db: Session,
    *,
    grain: str,
    columns: list[str],
    filters: dict | None = None,
    sort_field: str | None = None,
    sort_dir: str = "asc",
) -> dict:
    grain, cols = normalize_columns(grain, columns or [])
    if not cols:
        raise ValueError("No valid columns selected")

    full_rows, options = fetch_rows(db, grain, filters or {})
    sorted_rows = _sort_rows(full_rows, sort_field, sort_dir)
    projected = [_project_row(r, cols) for r in sorted_rows]

    column_meta = [{"key": k, "label": field_label(grain, k)} for k in cols]

    return {
        "today": date.today().isoformat(),
        "grain": grain,
        "mode": "master" if grain == MASTER_GRAIN else "legacy",
        "columns": column_meta,
        "filters": filters or {},
        "options": options,
        "row_count": len(projected),
        "rows": projected,
        "sort": {"field": sort_field, "dir": sort_dir or "asc"},
    }
