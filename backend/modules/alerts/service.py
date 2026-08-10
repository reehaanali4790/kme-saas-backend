"""Business logic for Price Alerts, extracted from modules/alerts/router.py as part
of the Phase 4 module rollout.

Also fixes a pre-existing bug carried over from before the modules/ restructure: the
WhatsApp endpoints imported `from services import whatsapp_service`, but the flat
`services/` package no longer exists (it moved to infrastructure/whatsapp/) - those
four endpoints would 500 with ModuleNotFoundError if ever called. Fixed here by
importing from infrastructure.whatsapp.whatsapp_service directly.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from core.exceptions import NotFoundError
from core.schemas import PaginationParams
from models.database_models import PriceAlert, LCMaster, LCProduct, User
from modules.alerts.schemas import TakeActionRequest


def _alert_row(alert: PriceAlert, lc: LCMaster, product: LCProduct) -> dict:
    return {
        "alert_id": alert.alert_id,
        "lc_number": lc.lc_number,
        "product_code": product.product_code,
        "origin": product.origin,
        "alert_type": alert.alert_type,
        "priority": alert.priority,
        "lc_price": float(alert.lc_price) if alert.lc_price else 0,
        "old_lme_price": float(alert.old_lme_price) if alert.old_lme_price else None,
        "new_lme_price": float(alert.new_lme_price) if alert.new_lme_price else None,
        "difference": float(alert.difference) if alert.difference else None,
        "difference_percent": float(alert.difference_percent) if alert.difference_percent else None,
        "total_impact": alert.total_impact,
        "alert_date": alert.alert_date,
        "viewed": alert.viewed,
        "action_taken": alert.action_taken,
        "days_remaining": lc.days_remaining,
    }


def list_alerts(db: Session, alert_type: Optional[str], viewed: Optional[bool],
                priority: Optional[str], origin: Optional[list[str]], product_code: Optional[list[str]],
                date_from: Optional[str], date_to: Optional[str], params: PaginationParams,
                search: Optional[str] = None) -> dict:
    query = db.query(PriceAlert).join(
        LCProduct, PriceAlert.line_id == LCProduct.line_id
    ).join(
        LCMaster, PriceAlert.lc_id == LCMaster.lc_id
    )

    if alert_type:
        query = query.filter(PriceAlert.alert_type == alert_type.upper())
    if viewed is not None:
        query = query.filter(PriceAlert.viewed == viewed)
    if priority:
        query = query.filter(PriceAlert.priority == priority.upper())
    if origin:
        normalized_origin = []
        for o in origin:
            if o:
                for part in str(o).split(","):
                    p = part.strip()
                    if p:
                        normalized_origin.append(p.upper())
        if normalized_origin:
            query = query.filter(LCProduct.origin.in_(normalized_origin))
    if product_code:
        normalized_prod = []
        for pc in product_code:
            if pc:
                for part in str(pc).split(","):
                    p = part.strip()
                    if p:
                        normalized_prod.append(p.upper())
        if normalized_prod:
            query = query.filter(LCProduct.product_code.in_(normalized_prod))
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            LCMaster.lc_number.ilike(term) | LCProduct.product_code.ilike(term)
        )
    if date_from:
        try:
            query = query.filter(PriceAlert.alert_date >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(PriceAlert.alert_date <= datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
        except ValueError:
            pass

    total = query.count()
    query = query.options(joinedload(PriceAlert.lc), joinedload(PriceAlert.product))
    alerts = query.order_by(PriceAlert.alert_date.desc()).offset(params.offset).limit(params.page_size).all()

    items = []
    for alert in alerts:
        if alert.lc and alert.product:
            items.append(_alert_row(alert, alert.lc, alert.product))
    return {
        "items": items,
        "total": total,
        "page": params.page,
        "page_size": params.page_size
    }


def savings_opportunities(db: Session, min_impact: Optional[float]) -> list:
    """Get all savings opportunities (price decreases)"""
    query = db.query(PriceAlert).join(
        LCMaster, PriceAlert.lc_id == LCMaster.lc_id
    ).filter(
        PriceAlert.alert_type == 'DECREASE',
        LCMaster.status == 'OPEN'
    ).options(
        joinedload(PriceAlert.lc),
        joinedload(PriceAlert.product)
    )

    if min_impact:
        query = query.filter(
            PriceAlert.difference.isnot(None),
            PriceAlert.quantity.isnot(None),
            PriceAlert.difference * PriceAlert.quantity <= -min_impact
        )

    alerts = query.order_by(
        (PriceAlert.difference * PriceAlert.quantity).asc()  # Most savings first
    ).all()

    result = []
    for alert in alerts:
        lc = alert.lc
        if lc and lc.days_remaining and lc.days_remaining > 0:
            product = alert.product
            if product:
                result.append(_alert_row(alert, lc, product))
    return result


def get_alert_or_404(db: Session, alert_id: int) -> PriceAlert:
    alert = db.query(PriceAlert).filter(PriceAlert.alert_id == alert_id).first()
    if not alert:
        raise NotFoundError("Alert not found")
    return alert


def mark_viewed(db: Session, alert_id: int, user_id: int):
    alert = get_alert_or_404(db, alert_id)
    alert.viewed = True
    alert.viewed_at = datetime.utcnow()
    alert.viewed_by = user_id
    db.commit()


def take_action(db: Session, alert_id: int, data: TakeActionRequest, user_id: int) -> PriceAlert:
    alert = get_alert_or_404(db, alert_id)

    alert.action_taken = data.action
    alert.action_date = datetime.utcnow()
    alert.action_by = user_id
    alert.action_notes = data.notes

    if not alert.viewed:
        alert.viewed = True
        alert.viewed_at = datetime.utcnow()
        alert.viewed_by = user_id

    db.commit()
    return alert


def alert_statistics(db: Session) -> dict:
    total = db.query(PriceAlert).count()
    unviewed = db.query(PriceAlert).filter(PriceAlert.viewed == False).count()
    decreases = db.query(PriceAlert).filter(PriceAlert.alert_type == 'DECREASE').count()
    increases = db.query(PriceAlert).filter(PriceAlert.alert_type == 'INCREASE').count()
    high_priority = db.query(PriceAlert).filter(PriceAlert.priority == 'HIGH').count()
    actioned = db.query(PriceAlert).filter(PriceAlert.action_taken.isnot(None)).count()
    whatsapp_sent = db.query(PriceAlert).filter(PriceAlert.whatsapp_sent == True).count()

    return {
        "total": total,
        "unviewed": unviewed,
        "by_type": {"DECREASE": decreases, "INCREASE": increases},
        "high_priority": high_priority,
        "actioned": actioned,
        "whatsapp_sent": whatsapp_sent,
    }


def dismiss_all(db: Session, user_id: int) -> int:
    """Mark all unread alerts as viewed. Returns the count dismissed."""
    alerts = db.query(PriceAlert).filter(PriceAlert.viewed == False).all()
    now = datetime.utcnow()
    for alert in alerts:
        alert.viewed = True
        alert.viewed_at = now
        alert.viewed_by = user_id
    db.commit()
    return len(alerts)


def alert_detail(db: Session, alert_id: int) -> dict:
    alert = get_alert_or_404(db, alert_id)

    lc = db.query(LCMaster).filter(LCMaster.lc_id == alert.lc_id).first()
    product = db.query(LCProduct).filter(LCProduct.line_id == alert.line_id).first()

    action_by_name = None
    if alert.action_by:
        action_user = db.query(User).filter(User.user_id == alert.action_by).first()
        if action_user:
            action_by_name = action_user.full_name

    return {
        "alert_id": alert.alert_id,
        "alert_date": alert.alert_date,
        "alert_type": alert.alert_type,
        "priority": alert.priority,
        "lc": {
            "lc_id": lc.lc_id,
            "lc_number": lc.lc_number,
            "supplier_name": lc.supplier_name,
            "lc_date": lc.lc_date,
            "status": lc.status,
            "days_remaining": lc.days_remaining
        } if lc else None,
        "product": {
            "line_id": product.line_id,
            "product_code": product.product_code,
            "product_name": product.product_name,
            "origin": product.origin,
            "quality": product.quality,
            "quantity": float(product.quantity)
        } if product else None,
        "prices": {
            "lc_price": float(alert.lc_price) if alert.lc_price else None,
            "old_lme_price": float(alert.old_lme_price) if alert.old_lme_price else None,
            "new_lme_price": float(alert.new_lme_price) if alert.new_lme_price else None,
            "difference": float(alert.difference) if alert.difference else None,
            "difference_percent": float(alert.difference_percent) if alert.difference_percent else None,
            "total_impact": alert.total_impact
        },
        "status": {
            "viewed": alert.viewed,
            "viewed_at": alert.viewed_at,
            "action_taken": alert.action_taken,
            "action_date": alert.action_date,
            "action_by": action_by_name,
            "action_notes": alert.action_notes
        },
        "whatsapp": {
            "sent": alert.whatsapp_sent,
            "sent_at": alert.whatsapp_sent_at,
            "delivery_status": alert.whatsapp_delivery_status
        }
    }
