"""Organization access checks for auth and tenant context."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status

from models.platform_models import Organization


def assert_org_accessible(org: Organization) -> None:
    """Block login/API access for suspended, archived, or expired trial orgs."""
    if org.status in ("suspended", "archived"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Organization '{org.name}' is {org.status} and cannot be accessed",
        )
    if org.status == "trial" and org.trial_ends_at and org.trial_ends_at < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trial period has expired. Please contact support to upgrade your plan.",
        )
    if org.status not in ("active", "trial", "pending"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Organization '{org.name}' is {org.status} and cannot be accessed",
        )
