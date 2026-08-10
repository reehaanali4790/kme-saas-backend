"""Pydantic schemas for Price Alerts (modules/alerts/router.py).

Already Pydantic models in the original file - moved here unchanged as part of
extracting the business logic into alerts/service.py.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AlertResponse(BaseModel):
    alert_id: int
    lc_number: str
    product_code: str
    origin: str
    alert_type: str
    priority: str
    lc_price: float
    old_lme_price: Optional[float]
    new_lme_price: Optional[float]
    difference: Optional[float]
    difference_percent: Optional[float]
    total_impact: Optional[float]
    alert_date: datetime
    viewed: bool
    action_taken: Optional[str]
    days_remaining: Optional[int]

    class Config:
        from_attributes = True


class TakeActionRequest(BaseModel):
    action: str  # REOPEN_REQUESTED, IGNORED, NOTED, UNDER_REVIEW
    notes: Optional[str] = None


class WhatsAppTestRequest(BaseModel):
    to: str  # local or international phone number, e.g. "0300xxxxxxx" or "+92300xxxxxxx"
    country_code: Optional[str] = None  # prepended to `to` if it isn't already international


class WhatsAppConfigOut(BaseModel):
    config_id: int
    recipient_name: str
    phone_number: str
    country_code: Optional[str]
    alert_type: Optional[str]
    active: Optional[bool]
    last_message_sent: Optional[datetime]
    total_messages_sent: Optional[int]

    class Config:
        from_attributes = True


class WhatsAppConfigUpsert(BaseModel):
    recipient_name: str
    phone_number: str
    country_code: Optional[str] = "+92"
    active: bool = True
