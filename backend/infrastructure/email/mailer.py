"""SMTP helper — no-op when SMTP_HOST is unset (logs instead)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config.settings import settings

logger = logging.getLogger(__name__)


def send_email(to_address: str, subject: str, body: str) -> bool:
    if not to_address:
        return False
    if not settings.SMTP_HOST:
        logger.info("SMTP not configured — would send to %s: %s", to_address, subject)
        logger.debug("Email body:\n%s", body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to_address
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        logger.info("Sent email to %s (%s)", to_address, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_address)
        return False
