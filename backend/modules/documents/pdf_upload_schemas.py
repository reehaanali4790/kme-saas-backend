"""Pydantic schemas for FastMarkets bulletin upload (modules/documents/pdf_upload_router.py).

UploadBulletinPayload carries prices already parsed by /check-rates's call to
extract_prices_from_pdf, so /upload-bulletin never re-parses the PDF - the frontend
holds the raw file only for step 1 and sends structured data from then on.
"""
from typing import List, Optional

from pydantic import BaseModel


class BulletinPriceRow(BaseModel):
    symbol: str
    low: float
    high: float
    avg: float


class UploadBulletinPayload(BaseModel):
    file_name: str
    bulletin_date: str
    prices: List[BulletinPriceRow]
    symbols_found: List[str]
    rate_id: Optional[int] = None
    usd_rate: Optional[float] = None
    eur_rate: Optional[float] = None
