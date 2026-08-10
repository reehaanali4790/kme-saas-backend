"""Read-side queries for the thehelpers.pk auto-synced (source='WEB') LME data and
its sync execution history. Kept separate from lme_rates_service.py, which builds the
FormulaEngine-driven region matrix for manually uploaded (FastMarkets) bulletins -
web-synced rows have no regional dimension and are never mixed into that matrix.
"""
from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.database_models import LMEBulletin, LMEPriceHistory, LMESyncRun


def get_latest_web_rates(db: Session, group: str | None = None) -> dict:
    bulletin = None
    if group:
        bulletin = (
            db.query(LMEBulletin)
            .join(LMEPriceHistory, LMEPriceHistory.bulletin_id == LMEBulletin.bulletin_id)
            .filter(LMEBulletin.source == 'WEB', LMEPriceHistory.product_code == group.upper())
            .order_by(desc(LMEBulletin.bulletin_date))
            .first()
        )
    if not bulletin:
        bulletin = (
            db.query(LMEBulletin)
            .filter(LMEBulletin.source == 'WEB')
            .order_by(desc(LMEBulletin.bulletin_date))
            .first()
        )
    # Deliberately no further fallback to non-WEB bulletins here: this panel is
    # badged "Auto Synced" specifically because it's thehelpers.pk data - silently
    # showing a manual/FastMarkets bulletin instead would mislabel its source.
    if not bulletin:
        return {"bulletin_date": None, "rows": []}

    query = db.query(LMEPriceHistory).filter(LMEPriceHistory.bulletin_id == bulletin.bulletin_id)
    if group:
        group_rows = query.filter(LMEPriceHistory.product_code == group.upper()).all()
        rows = group_rows if group_rows else query.all()
    else:
        rows = query.all()

    rows.sort(key=lambda r: (r.product_code or '', r.quality or '', r.symbol or ''))

    return {
        "bulletin_date": bulletin.bulletin_date.isoformat(),
        "rows": [
            {
                "hs_code": r.symbol,
                "group": r.product_code,
                "quality": r.quality,
                "alloy": r.origin,
                "price": float(r.avg_price) if r.avg_price is not None else None,
            }
            for r in rows
        ],
    }


def get_sync_history(db: Session, limit: int = 50) -> list[dict]:
    runs = db.query(LMESyncRun).order_by(desc(LMESyncRun.started_at)).limit(limit).all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "trigger": r.trigger,
            "triggered_by": r.triggered_by,
            "bulletin_date_found": r.bulletin_date_found.isoformat() if r.bulletin_date_found else None,
            "source_url": r.source_url,
            "error_message": r.error_message,
        }
        for r in runs
    ]
