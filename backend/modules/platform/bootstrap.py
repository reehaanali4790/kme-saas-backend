"""Platform owner bootstrap — create/promote SaaS super-admin accounts."""

import logging

from sqlalchemy.orm import Session

from config.settings import settings
from models.platform_models import Organization, User
from modules.auth.services import AuthService

logger = logging.getLogger(__name__)

DEFAULT_OWNER_USERNAME = "platform-owner"


def _owner_emails() -> list[str]:
    raw = settings.PLATFORM_ADMIN_EMAILS or ""
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def sync_platform_admin_emails(db: Session) -> int:
    """Promote users listed in PLATFORM_ADMIN_EMAILS to platform super-admin."""
    emails = _owner_emails()
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


def ensure_platform_owner_user(db: Session, default_org: Organization | None) -> User | None:
    """
    Create the SaaS owner account when PLATFORM_ADMIN_PASSWORD is set.

    Uses PLATFORM_ADMIN_USERNAME (default platform-owner), first email in
    PLATFORM_ADMIN_EMAILS, and PLATFORM_ADMIN_FULL_NAME. Idempotent: existing
    users are promoted to platform admin and given default-org membership only.
    Password is applied only when the user is first created.
    """
    password = (settings.PLATFORM_ADMIN_PASSWORD or "").strip()
    if not password:
        return None

    emails = _owner_emails()
    if not emails:
        logger.warning(
            "PLATFORM_ADMIN_PASSWORD is set but PLATFORM_ADMIN_EMAILS is empty — "
            "cannot create platform owner"
        )
        return None

    email = emails[0]
    username = (settings.PLATFORM_ADMIN_USERNAME or DEFAULT_OWNER_USERNAME).strip()
    full_name = (settings.PLATFORM_ADMIN_FULL_NAME or "Platform Owner").strip()

    user = db.query(User).filter(
        (User.email.ilike(email)) | (User.username == username)
    ).first()

    created = False
    if not user:
        user = AuthService.create_user(
            db,
            username=username,
            email=email,
            password=password,
            full_name=full_name,
            is_platform_admin=True,
        )
        created = True
        logger.info("Created platform owner user: %s (%s)", username, email)
    else:
        if not user.is_platform_admin:
            user.is_platform_admin = True
        if user.email.lower() != email:
            user.email = email
        if user.full_name != full_name:
            user.full_name = full_name
        db.commit()
        logger.info("Platform owner already exists: %s — ensured admin flag", user.username)

    if default_org:
        membership = AuthService.get_user_memberships(db, user.user_id)
        org_ids = {m.organization_id for m in membership}
        if default_org.organization_id not in org_ids:
            AuthService.add_membership(
                db,
                user.user_id,
                default_org.organization_id,
                "ADMIN",
                is_default=len(membership) == 0,
            )
            logger.info(
                "Added platform owner to org %s (%s)",
                default_org.slug,
                default_org.schema_name,
            )

    if created:
        logger.info(
            "Platform owner ready — username=%s email=%s (password from PLATFORM_ADMIN_PASSWORD)",
            user.username,
            user.email,
        )
    return user
