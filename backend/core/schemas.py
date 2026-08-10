"""Shared response schemas.

Existing endpoints return bare lists / ad hoc per-handler dicts inconsistently
(no shared envelope or pagination convention). This is the one shared shape for
NEW/migrated list endpoints to use going forward, starting with the bank_limits
reference migration - it is not retrofitted onto untouched endpoints, since that
would be a breaking response-shape change for the (currently untested) frontend.
"""
from typing import Generic, List, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """FastAPI query-param dependency: Depends(PaginationParams)."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PaginatedResponse(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    page_size: int

    @classmethod
    def create(cls, items: List[T], total: int, params: PaginationParams) -> "PaginatedResponse[T]":
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)
