"""Pydantic schema for the AI Assistant endpoint (modules/admin/assistant_endpoints.py)."""
from typing import Optional

from pydantic import BaseModel


class AskRequest(BaseModel):
    question: Optional[str] = None
