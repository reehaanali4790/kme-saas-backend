"""Auto-ETA estimation — etd + transit_days business days.

"Business days" here means Mon-Fri (matches the standard banking week this app already uses
elsewhere); it does not skip public holidays.

This is a pure formula with no opinion on *when* it's allowed to write shipment.eta - see
Shipment.eta_source for the AUTO/WEBSITE/MANUAL precedence rules enforced by the callers
(modules/shipments/bl_service.py, modules/shipments/services.py).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

DEFAULT_TRANSIT_DAYS = 25


def add_business_days(start: date, n: int) -> date:
    """Add n business days (Mon-Fri) to start. Negative n walks backward."""
    d = start
    remaining = abs(n)
    step = 1 if n >= 0 else -1
    while remaining > 0:
        d += timedelta(days=step)
        if d.weekday() < 5:  # Monday=0 ... Friday=4
            remaining -= 1
    return d


def estimate_eta(etd: Optional[date], transit_days: Optional[int]) -> Optional[date]:
    """etd + transit_days business days, or None if etd isn't known yet."""
    if not etd:
        return None
    return add_business_days(etd, transit_days if transit_days is not None else DEFAULT_TRANSIT_DAYS)
