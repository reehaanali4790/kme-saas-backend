"""Schemas for shipment record-keeping documents (DPL + other supporting docs)."""
from typing import Optional

from pydantic import BaseModel, field_validator


class ShipmentDocStageResult(BaseModel):
    staged_file: str
    original_filename: str
    existing_doc_id: Optional[int] = None


class ShipmentDocSave(BaseModel):
    shipment_id: int
    kind: str = "OTHER"
    name: Optional[str] = None
    staged_file: str
    original_filename: Optional[str] = None
    doc_id: Optional[int] = None

    @field_validator("kind", mode="before")
    @classmethod
    def _upper_kind(cls, v):
        return (str(v or "OTHER")).upper()
