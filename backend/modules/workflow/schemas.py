"""Workflow API schemas."""
from typing import Optional

from pydantic import BaseModel, Field


class WorkflowOverride(BaseModel):
    override_reason: Optional[str] = Field(None, min_length=3, max_length=500)


class GDStatusUpdateWithOverride(BaseModel):
    status: str
    override_reason: Optional[str] = Field(None, min_length=3, max_length=500)
