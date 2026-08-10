"""Application-level exception hierarchy.

Route/service code raises these instead of the ad hoc `raise HTTPException(404, ...)`
repeated per-endpoint across the codebase. A single handler in main.py maps them to
HTTP responses, alongside (not replacing) the existing SQLAlchemy/HTTPException
handlers - same "log detail server-side, return a clean message" pattern.
"""
from typing import Optional


class AppError(Exception):
    """Base class for application errors that should become a specific HTTP response."""

    status_code = 500
    default_detail = "An unexpected error occurred."

    def __init__(self, detail: Optional[str] = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    default_detail = "The requested resource was not found."


class ConflictError(AppError):
    status_code = 409
    default_detail = "The request conflicts with the current state of the resource."


class ForbiddenError(AppError):
    status_code = 403
    default_detail = "You do not have permission to perform this action."


class ValidationError(AppError):
    status_code = 422
    default_detail = "The request could not be validated."
