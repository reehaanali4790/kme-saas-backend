"""Business logic for Currency Rates, extracted from modules/currency_rates/router.py
as part of the Phase 4 module rollout.

main.py's scheduled NBP job (`_nbp_daily_job`) imports `upsert_nbp_rate` from here (it
used to import it from modules.currency_rates.router directly) - keep that name stable.

The "already exists for this date" / "EUR rate must be greater than USD rate" checks
raise plain ValueError (not an AppError) since they map to the original's
HTTPException(400) - no AppError subclass models a generic 400, so the router
translates ValueError -> HTTPException(400) itself (same approach as
modules/documents/pdf_upload_service.py's resolve_currency_rate).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError
from models.database_models import CurrencyRate
from infrastructure.audit.audit_service import log_audit
from modules.currency_rates.schemas import CurrencyRateCreate, CurrencyRateUpdate


def upsert_nbp_rate(db: Session, data: dict, user_id=None) -> dict:
    """Insert or update the CurrencyRate for the NBP sheet date. Returns a status dict."""
    sheet_date = data["sheet_date"]
    existing = db.query(CurrencyRate).filter(CurrencyRate.rate_date == sheet_date).first()
    if existing:
        existing.usd_rate = data["usd"]
        if data.get("eur"):
            existing.eur_rate = data["eur"]
        if data.get("aed"):
            existing.aed_rate = data["aed"]
        existing.source = "NBP"
        existing.notes = f"Auto-fetched from NBP ratesheet ({data.get('source_url','')})"
        existing.updated_at = datetime.now()
        existing.updated_by = user_id
        action = "updated"
        rate = existing
    else:
        rate = CurrencyRate(
            rate_date=sheet_date, usd_rate=data["usd"],
            eur_rate=data.get("eur") or data["usd"], aed_rate=data.get("aed"),
            source="NBP",
            notes=f"Auto-fetched from NBP ratesheet ({data.get('source_url','')})",
            created_by=user_id,
        )
        db.add(rate)
        action = "created"
    db.commit()
    db.refresh(rate)
    return {
        "action": action, "rate_id": rate.rate_id, "rate_date": str(rate.rate_date),
        "usd_rate": float(rate.usd_rate), "eur_rate": float(rate.eur_rate),
        "aed_rate": float(rate.aed_rate) if rate.aed_rate else None,
    }


def rate_to_dict(rate: CurrencyRate, with_created_at: bool = False) -> dict:
    out = {
        "rate_id": rate.rate_id,
        "rate_date": str(rate.rate_date),
        "usd_rate": float(rate.usd_rate),
        "eur_rate": float(rate.eur_rate),
        "aed_rate": float(rate.aed_rate) if rate.aed_rate else None,
        "source": rate.source,
        "notes": rate.notes,
    }
    if with_created_at:
        out["created_at"] = rate.created_at.isoformat() if rate.created_at else None
    return out


def list_rates(db: Session, limit: int) -> list:
    rates = db.query(CurrencyRate).order_by(desc(CurrencyRate.rate_date)).limit(limit).all()
    return [rate_to_dict(r, with_created_at=True) for r in rates]


def get_rate_by_date(db: Session, rate_date) -> Optional[CurrencyRate]:
    return db.query(CurrencyRate).filter(CurrencyRate.rate_date == rate_date).first()


def get_latest_rate(db: Session) -> Optional[CurrencyRate]:
    return db.query(CurrencyRate).order_by(desc(CurrencyRate.rate_date)).first()


def create_rate(db: Session, data: CurrencyRateCreate, user_id: int) -> CurrencyRate:
    existing = db.query(CurrencyRate).filter(CurrencyRate.rate_date == data.rate_date).first()
    if existing:
        raise ValueError(f"Currency rate already exists for {data.rate_date}")

    if data.eur_rate <= data.usd_rate:
        raise ValueError("EUR rate must be greater than USD rate")

    new_rate = CurrencyRate(
        rate_date=data.rate_date,
        usd_rate=data.usd_rate,
        eur_rate=data.eur_rate,
        source=data.source or "Manual",
        notes=data.notes,
        created_by=user_id
    )
    db.add(new_rate)
    db.flush()
    log_audit(db, user_id, "CREATE_CURRENCY_RATE", entity_type="CURRENCY_RATE", entity_id=new_rate.rate_id,
              new_value={"rate_date": str(new_rate.rate_date), "usd_rate": str(new_rate.usd_rate),
                        "eur_rate": str(new_rate.eur_rate)},
              description=f"Created currency rate for {new_rate.rate_date}")
    db.commit()
    db.refresh(new_rate)
    return new_rate


def update_rate(db: Session, rate_id: int, data: CurrencyRateUpdate, user_id: int) -> CurrencyRate:
    rate = db.query(CurrencyRate).filter(CurrencyRate.rate_id == rate_id).first()
    if not rate:
        raise NotFoundError("Currency rate not found")

    old_value = {"usd_rate": str(rate.usd_rate), "eur_rate": str(rate.eur_rate), "source": rate.source}

    if data.usd_rate is not None:
        rate.usd_rate = data.usd_rate
    if data.eur_rate is not None:
        rate.eur_rate = data.eur_rate
    if data.source is not None:
        rate.source = data.source
    if data.notes is not None:
        rate.notes = data.notes

    if rate.eur_rate <= rate.usd_rate:
        raise ValueError("EUR rate must be greater than USD rate")

    rate.updated_at = datetime.now()
    rate.updated_by = user_id
    log_audit(db, user_id, "UPDATE_CURRENCY_RATE", entity_type="CURRENCY_RATE", entity_id=rate.rate_id,
              old_value=old_value,
              new_value={"usd_rate": str(rate.usd_rate), "eur_rate": str(rate.eur_rate), "source": rate.source},
              description=f"Updated currency rate for {rate.rate_date}")
    db.commit()
    return rate


def delete_rate(db: Session, rate_id: int, deleted_by: int):
    rate = db.query(CurrencyRate).filter(CurrencyRate.rate_id == rate_id).first()
    if not rate:
        raise NotFoundError("Currency rate not found")
    # Check if rate is being used by any bulletin
    # (This would require checking lme_bulletins.rate_id)
    # For now, allow deletion
    log_audit(db, deleted_by, "DELETE_CURRENCY_RATE", entity_type="CURRENCY_RATE", entity_id=rate.rate_id,
              old_value={"rate_date": str(rate.rate_date), "usd_rate": str(rate.usd_rate)},
              description=f"Deleted currency rate for {rate.rate_date}")
    db.delete(rate)
    db.commit()
