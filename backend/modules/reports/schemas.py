"""Pydantic schemas for the Reports module (modules/reports/router.py).

vessel/eta/port_status are kept as raw optional strings (no lenient/strict type
validators) - the original hand-validates each field with its own custom error
message ("vessel is required", "eta must be YYYY-MM-DD", "Provide eta and/or
port_status to update."), so the parsing + validation stays in
reports/service.py's bulk_update_vessel(), which raises ValueError for the router
to translate into HTTPException(400, str(e)) - preserving the exact original
status code and message text (see modules/currency_rates/service.py's docstring
for why plain ValueError is used instead of an AppError subclass here).
"""
from typing import Optional

from pydantic import BaseModel


class VesselBulkUpdate(BaseModel):
    vessel: Optional[str] = None
    eta: Optional[str] = None
    port_status: Optional[str] = None
    berth: Optional[str] = None
    on_port_date: Optional[str] = None
    departure_date: Optional[str] = None
