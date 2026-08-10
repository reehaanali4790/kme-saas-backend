"""Pydantic schemas for demurrage config (modules/shipments/demurrage_router.py).

Unlike most other migrated modules, the original update_config() already validated
strictly (raising HTTPException(400) on a malformed free_days/per_day_charge/warn_days)
rather than silently tolerating bad input - so this schema uses Pydantic's normal strict
int/Decimal typing with no lenient before-validators; a malformed value now surfaces as a
clean 422 via main.py's RequestValidationError handler instead of a hand-rolled 400.
"""
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class DemurrageConfigUpdate(BaseModel):
    free_days: Optional[int] = None
    per_day_charge: Optional[Decimal] = None
    currency: Optional[str] = None
    warn_days: Optional[int] = None


class DemurrageAmountUpdate(BaseModel):
    """Record actual demurrage paid — only allowed once shipment is delivered / cleared."""
    demurrage_total_amount: Optional[Decimal] = None
    demurrage_currency: Optional[str] = None
