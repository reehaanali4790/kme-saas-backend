"""Pydantic schemas for branding config (modules/branding/router.py)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BrandingConfigOut(BaseModel):
    app_name: str
    logo_bg_color: str
    logo_url: Optional[str] = None
    updated_at: Optional[datetime] = None


class BrandingConfigUpdate(BaseModel):
    app_name: Optional[str] = Field(None, min_length=1, max_length=120)
    logo_bg_color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
