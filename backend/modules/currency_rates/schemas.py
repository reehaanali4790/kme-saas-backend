"""Pydantic schemas for Currency Rates (modules/currency_rates/router.py).

These were already Pydantic models in the original file (this module was already
ahead of the raw-dict-body pattern used elsewhere) - moved here unchanged as part
of extracting the business logic into currency_service.py.
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class CurrencyRateCreate(BaseModel):
    rate_date: date
    usd_rate: float = Field(gt=0, description="USD to PKR rate")
    eur_rate: float = Field(gt=0, description="EUR to PKR rate")
    source: Optional[str] = "Manual"
    notes: Optional[str] = None


class CurrencyRateUpdate(BaseModel):
    usd_rate: Optional[float] = Field(None, gt=0)
    eur_rate: Optional[float] = Field(None, gt=0)
    source: Optional[str] = None
    notes: Optional[str] = None
