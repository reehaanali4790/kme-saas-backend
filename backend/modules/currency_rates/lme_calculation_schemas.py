"""Pydantic schemas for LME calculation (modules/currency_rates/lme_calculation_router.py).

Already Pydantic models in the original file - moved here unchanged as part of
extracting the business logic into lme_calculation_service.py.
"""
from typing import List, Optional

from pydantic import BaseModel


class LCForCalculation(BaseModel):
    lc_id: int
    lc_number: str
    lc_date: Optional[str]
    status: Optional[str]
    product_code: Optional[str]
    origin: Optional[str]
    quality: Optional[str]
    formula_number: Optional[int]
    current_lme: Optional[float]
    baseline_lme: Optional[float]
    lc_unit_price: Optional[float]
    difference: Optional[float]


class FormulaTestRequest(BaseModel):
    product_code: str
    origin: str
    quality: str
    grade: Optional[str] = None
    product_name: Optional[str] = None


class FormulaTestResponse(BaseModel):
    formula_number: int
    formula_name: str
    required_symbols: List[str]


class ApplyRatesRequest(BaseModel):
    bulletin_id: int
    lc_ids: List[int] = []   # empty list = apply to all LCs in 40-day window
