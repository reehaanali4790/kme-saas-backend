"""Pydantic schema for the shared name-master lookup endpoint
(modules/reports/lookup_router.py)."""
from typing import Optional

from pydantic import BaseModel


class LookupCreate(BaseModel):
    name: Optional[str] = None
