"""Sync platform owner emails from settings to is_platform_admin."""

import logging

from sqlalchemy.orm import Session

from config.settings import settings
from models.platform_models import User

logger = logging.getLogger(__name__)


def sync_platform_admin_emails(db: Session) -> int:
    """Promote users listed in PLATFORM_ADMIN_EMAILS to platform super-admin."""
    raw = settings.PLATFORM_ADMIN_EMAILS or ""
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not emails:
        return 0

    promoted = 0
    for email in emails:
        user = db.query(User).filter(User.email.ilike(email)).first()
        if user and not user.is_platform_admin:
            user.is_platform_admin = True
            promoted += 1
            logger.info("Promoted platform admin: %s", user.email)

    if promoted:
        db.commit()
    return promoted
