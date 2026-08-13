"""Contract business logic - field/line-item application, party-name normalization,
response assembly, and LC linking.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload, selectinload
from config.settings import settings
from core.exceptions import NotFoundError
from models.database_models import Contract, ContractItem, LCMaster
from infrastructure.normalization.normalization_service import (
    normalize_contract_parties, normalize_item, norm_bank,
)
from infrastructure.audit.audit_service import log_audit
from utils.parsing import parse_date
from utils.uploads import safe_upload_path
from .schemas import BankPerformanceRow, ContractLineItemIn, ContractLineItemOut, ContractOut, ContractSave

UPLOAD_DIR = os.path.join(settings.UPLOAD_DIR, "contract_documents")
STAGE_DIR = os.path.join(UPLOAD_DIR, "_staged")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}

# Column max lengths for String fields — AI extraction often exceeds these and
# Postgres then raises StringDataRightTruncation → opaque 500 on save.
_FIELD_MAX_LEN = {
    "contract_number": 50,
    "supplier_name": 300,
    "buyer_name": 300,
    "indentor_name": 300,
    "currency": 10,
    "delivery_terms": 120,
    "country_of_origin": 100,
    "buyer_ntn": 20,
    "port_of_loading": 200,
    "shipping_mark": 100,
    "hs_code": 50,
    "beneficiary_bank": 200,
    "beneficiary_swift": 20,
    "beneficiary_account": 50,
    "bank_name": 200,
}


def _clip(value, max_len: int):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= max_len else s[:max_len]


def safe_remove(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def apply_fields(c: Contract, data: ContractSave) -> None:
    for field in (
        "contract_number", "supplier_name", "buyer_name", "indentor_name", "payment_terms",
        "currency", "delivery_terms", "country_of_origin", "buyer_ntn", "buyer_address",
        "supplier_address", "quantity_tolerance", "port_of_loading", "shipping_mark",
        "hs_code", "beneficiary_bank", "beneficiary_swift", "beneficiary_account",
        "beneficiary_bank_addr", "documents_required", "notes", "bank_name",
    ):
        value = getattr(data, field)
        if value is not None:
            max_len = _FIELD_MAX_LEN.get(field)
            setattr(c, field, _clip(value, max_len) if max_len else value)
    for field in ("contract_date", "valid_to"):
        value = getattr(data, field)
        if value is not None:
            setattr(c, field, value)
    if data.status:
        c.status = data.status
    c.updated_at = datetime.utcnow()


def apply_items(c: Contract, items: Optional[List[ContractLineItemIn]], db: Session) -> None:
    """Replace all line items from the payload (skips fully-empty rows).
    Mutates the c.items relationship (delete-orphan removes the old rows) so reads in the
    same session stay consistent."""
    if items is None:
        return
    c.items.clear()        # delete-orphan cascade removes the old rows
    db.flush()
    for n, item in enumerate(items, start=1):
        has_str = any([item.product_name, item.size_description, item.grade_description, item.mill_name])
        has_dec = any(v is not None for v in (
            item.weight_mt, item.lc_price, item.lc_amount, item.purchase_rate, item.purchase_amount
        ))
        if not has_str and not has_dec:
            continue
        code, review = normalize_item(item.product_name)
        c.items.append(ContractItem(
            line_no=item.line_no or n,
            item_code=code, item_review=bool(review),
            product_name=item.product_name, size_description=item.size_description,
            grade_description=item.grade_description, mill_name=item.mill_name, weight_mt=item.weight_mt,
            lc_price=item.lc_price, lc_amount=item.lc_amount,
            purchase_rate=item.purchase_rate, purchase_amount=item.purchase_amount,
        ))


def link_lc(c: Contract, lc_id: int, status_provided: bool, db: Session) -> None:
    """Attach the contract to an existing LC; default status ACTUAL unless caller set one."""
    lc = db.query(LCMaster).filter(LCMaster.lc_id == lc_id).first()
    if not lc:
        raise NotFoundError("LC not found")
    lc.contract_id = c.contract_id
    if not status_provided:
        c.status = "ACTUAL"


def item_to_schema(it: ContractItem) -> ContractLineItemOut:
    return ContractLineItemOut(
        item_id=it.item_id, line_no=it.line_no, item_code=it.item_code,
        item_review=bool(it.item_review), product_name=it.product_name,
        size_description=it.size_description, grade_description=it.grade_description,
        mill_name=it.mill_name,
        weight_mt=float(it.weight_mt) if it.weight_mt is not None else None,
        lc_price=float(it.lc_price) if it.lc_price is not None else None,
        lc_amount=float(it.lc_amount) if it.lc_amount is not None else None,
        purchase_rate=float(it.purchase_rate) if it.purchase_rate is not None else None,
        purchase_amount=float(it.purchase_amount) if it.purchase_amount is not None else None,
    )


def to_schema(c: Contract) -> ContractOut:
    # Bank timeline: sent-to-bank (upload) vs LC received (LC import_date) + days between.
    sent = c.sent_to_bank_at
    lc_received = c.lc.import_date if c.lc else None
    days_to_lc = (lc_received.date() - sent.date()).days if (sent and lc_received) else None
    items = sorted(c.items, key=lambda x: x.line_no or 0)

    return ContractOut(
        contract_id=c.contract_id,
        document_filename=c.document_filename,
        has_document=bool(c.document_path and os.path.exists(c.document_path)),
        source=c.source, status=c.status,
        lc_id=c.lc.lc_id if c.lc else None,
        lc_number=c.lc.lc_number if c.lc else None,
        sent_to_bank_at=sent, lc_received_at=lc_received, days_to_lc=days_to_lc,
        contract_number=c.contract_number, supplier_name=c.supplier_name,
        buyer_name=c.buyer_name, indentor_name=c.indentor_name,
        payment_terms=c.payment_terms, currency=c.currency,
        delivery_terms=c.delivery_terms, country_of_origin=c.country_of_origin,
        buyer_ntn=c.buyer_ntn, buyer_address=c.buyer_address,
        supplier_address=c.supplier_address, quantity_tolerance=c.quantity_tolerance,
        port_of_loading=c.port_of_loading, shipping_mark=c.shipping_mark,
        hs_code=c.hs_code, beneficiary_bank=c.beneficiary_bank,
        beneficiary_swift=c.beneficiary_swift, beneficiary_account=c.beneficiary_account,
        beneficiary_bank_addr=c.beneficiary_bank_addr,
        documents_required=c.documents_required, notes=c.notes, bank_name=c.bank_name,
        contract_date=c.contract_date, valid_to=c.valid_to,
        line_items=[item_to_schema(it) for it in items],
        total_weight_mt=float(sum((it.weight_mt or 0) for it in items)) if items else None,
        total_lc_amount=float(sum((it.lc_amount or 0) for it in items)) if items else None,
        total_purchase_amount=float(sum((it.purchase_amount or 0) for it in items)) if items else None,
    )


def bank_issuance_performance(db: Session, date_from: Optional[str] = None,
                               date_to: Optional[str] = None,
                               limit: Optional[int] = None) -> List[BankPerformanceRow]:
    """Each bank's historical average LC issuance turnaround: days between a contract
    being sent to the bank (sent_to_bank_at) and the bank issuing the LC (lc.import_date),
    averaged across every completed contract for that bank. Same per-contract formula as
    to_schema()'s days_to_lc, and the same grouping convention as
    reports/service.py:bank_report() (norm_bank + in-memory aggregation) — contracts have
    no bank FK, only a free-text bank_name, so grouping happens at read time. Cancelled
    contracts and contracts missing either date (not yet sent, or LC not yet received)
    are excluded — "completed" here means the full sent-to-issued cycle actually happened."""
    q = db.query(Contract).options(joinedload(Contract.lc)).filter(Contract.status != "CANCELLED")

    df, dt = parse_date(date_from), parse_date(date_to)
    if df:
        q = q.filter(Contract.sent_to_bank_at >= df)
    if dt:
        q = q.filter(Contract.sent_to_bank_at <= dt)

    banks: dict[str, list[int]] = {}
    for c in q.all():
        sent = c.sent_to_bank_at
        lc_received = c.lc.import_date if c.lc else None
        if not (sent and lc_received):
            continue
        days = (lc_received.date() - sent.date()).days
        if days < 0:
            continue
        banks.setdefault(norm_bank(c.bank_name), []).append(days)

    rows = [
        BankPerformanceRow(
            bank=canon,
            contract_count=len(days_list),
            avg_days_to_lc=round(sum(days_list) / len(days_list), 1),
            min_days_to_lc=min(days_list),
            max_days_to_lc=max(days_list),
        )
        for canon, days_list in banks.items()
    ]
    rows.sort(key=lambda r: r.avg_days_to_lc)
    return rows[:limit] if limit else rows


def get_contract_or_404(db: Session, contract_id: int) -> Contract:
    c = db.query(Contract).filter(Contract.contract_id == contract_id).first()
    if not c:
        raise NotFoundError("Contract not found")
    return c


def list_contracts(db: Session, status: Optional[str]) -> List[Contract]:
    # to_schema() reads c.lc and c.items for every row; neither was eager-loaded before,
    # so each contract triggered two extra lazy-load queries (N+1). lc is a scalar
    # (uselist=False) relationship so joinedload is safe there; items is a collection so
    # selectinload avoids a join fan-out.
    q = db.query(Contract).options(
        joinedload(Contract.lc),
        selectinload(Contract.items),
    )
    if status:
        q = q.filter(Contract.status == status.upper())
    return q.order_by(Contract.contract_id.desc()).all()


def lookup_contracts(db: Session, q: str, limit: int = 10) -> list[dict]:
    """Search contracts for LC intake / duplicate detection."""
    from modules.workflow.redirects import create_lc_href, lc_detail_href

    term = f"%{(q or '').strip()}%"
    if not term or term == "%%":
        return []
    rows = (
        db.query(Contract)
        .options(joinedload(Contract.lc))
        .filter(
            Contract.status != "CANCELLED",
            (Contract.contract_number.ilike(term))
            | (Contract.supplier_name.ilike(term))
            | (Contract.buyer_name.ilike(term)),
        )
        .order_by(Contract.contract_id.desc())
        .limit(limit)
        .all()
    )
    results = []
    for c in rows:
        lc = c.lc
        if lc:
            redirect_href = lc_detail_href(lc.lc_id)
            redirect_label = "View existing LC"
        else:
            redirect_href = create_lc_href(c.contract_id)
            redirect_label = "Open LC for this contract"
        results.append({
            "contract_id": c.contract_id,
            "contract_number": c.contract_number,
            "supplier_name": c.supplier_name,
            "buyer_name": c.buyer_name,
            "contract_date": c.contract_date.isoformat() if c.contract_date else None,
            "status": c.status,
            "lc_id": lc.lc_id if lc else None,
            "lc_number": lc.lc_number if lc else None,
            "has_lc": lc is not None,
            "redirect_href": redirect_href,
            "redirect_label": redirect_label,
        })
    return results


def find_duplicate_contract(db: Session, contract_number: Optional[str], supplier_name: Optional[str]) -> Optional[Contract]:
    num = (contract_number or "").strip()
    if not num:
        return None
    q = db.query(Contract).options(joinedload(Contract.lc)).filter(
        Contract.status != "CANCELLED",
        Contract.contract_number.ilike(num),
    )
    sup = (supplier_name or "").strip()
    if sup:
        q = q.filter(Contract.supplier_name.ilike(f"%{sup}%"))
    return q.order_by(Contract.contract_id.desc()).first()


def save_contract(db: Session, data: ContractSave, created_by: int) -> Contract:
    is_new = not data.contract_id
    if is_new:
        dup = find_duplicate_contract(db, data.contract_number, data.supplier_name)
        if dup:
            from fastapi import HTTPException
            from modules.workflow.pipeline_service import enrich_blocked_detail

            lc = dup.lc
            detail = enrich_blocked_detail(
                {
                    "code": "workflow_blocked",
                    "blocker": f"Contract {dup.contract_number} already exists"
                               + (f" (LC {lc.lc_number} opened)" if lc else " (awaiting LC)"),
                    "required_step": "lc_exists" if lc else "lc",
                    "existing_contract_id": dup.contract_id,
                },
                contract_id=dup.contract_id,
                lc_id=lc.lc_id if lc else None,
            )
            raise HTTPException(status_code=409, detail=detail)
    if data.contract_id:
        c = db.query(Contract).filter(Contract.contract_id == data.contract_id).first()
        if not c:
            raise NotFoundError("Contract placeholder not found")
    else:
        c = Contract(source="UPLOADED" if data.staged_file else "MANUAL", status="DRAFT", created_by=created_by)
        db.add(c)
        db.flush()
    apply_fields(c, data)
    if c.status == "DRAFT":
        c.status = "FINAL"
    apply_items(c, data.line_items, db)
    normalize_contract_parties(c, db)
    if is_new and data.staged_file:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        stage_path = os.path.join(STAGE_DIR, os.path.basename(data.staged_file))
        if os.path.exists(stage_path):
            orig = data.original_filename or ("contract" + Path(stage_path).suffix)
            try:
                dest = safe_upload_path(UPLOAD_DIR, c.contract_id, orig, ALLOWED_EXTENSIONS)
                os.replace(stage_path, dest)
                c.document_filename = orig
                c.document_path = dest
            except (OSError, ValueError) as exc:
                # Don't fail the whole save if the PDF can't be moved — contract row still matters.
                import logging
                logging.getLogger("uvicorn").error(
                    "Contract %s: failed to persist staged document %s: %s",
                    c.contract_id, stage_path, exc,
                )
        c.raw_extracted_data = data.raw_extracted_data
    # Contract sent to bank at upload/first-save time (only stamp once).
    if c.sent_to_bank_at is None:
        c.sent_to_bank_at = datetime.utcnow()
    if data.lc_id:
        link_lc(c, data.lc_id, status_provided=bool(data.status), db=db)
    log_audit(db, created_by, "CREATE_CONTRACT" if is_new else "SAVE_CONTRACT",
              entity_type="CONTRACT", entity_id=c.contract_id,
              description=f"{'Created' if is_new else 'Saved'} contract "
                          f"'{c.contract_number or c.contract_id}'")
    db.commit()
    db.refresh(c)
    return c


def update_contract(db: Session, contract_id: int, data: ContractSave, updated_by: int) -> Contract:
    c = get_contract_or_404(db, contract_id)
    apply_fields(c, data)
    apply_items(c, data.line_items, db)
    normalize_contract_parties(c, db)
    if data.lc_id:
        link_lc(c, data.lc_id, status_provided=bool(data.status), db=db)
    c.updated_by = updated_by
    log_audit(db, updated_by, "UPDATE_CONTRACT", entity_type="CONTRACT", entity_id=c.contract_id,
              description=f"Updated contract '{c.contract_number or c.contract_id}'")
    db.commit()
    return c


def set_status(db: Session, contract_id: int, status: str, updated_by: int) -> Contract:
    c = get_contract_or_404(db, contract_id)
    old_status = c.status
    c.status = status
    c.updated_by = updated_by
    c.updated_at = datetime.utcnow()
    log_audit(db, updated_by, "UPDATE_CONTRACT_STATUS", entity_type="CONTRACT", entity_id=c.contract_id,
              old_value={"status": old_status}, new_value={"status": status},
              description=f"Contract '{c.contract_number or c.contract_id}' status "
                          f"{old_status} -> {status}")
    db.commit()
    return c


def delete_contract(db: Session, contract_id: int, deleted_by: int) -> None:
    c = get_contract_or_404(db, contract_id)
    log_audit(db, deleted_by, "DELETE_CONTRACT", entity_type="CONTRACT", entity_id=c.contract_id,
              old_value={"contract_number": c.contract_number, "status": c.status},
              description=f"Deleted contract '{c.contract_number or c.contract_id}'")
    safe_remove(c.document_path)
    db.delete(c)
    db.commit()
