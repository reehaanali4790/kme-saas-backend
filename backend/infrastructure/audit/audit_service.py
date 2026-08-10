"""Audit trail helper for sensitive/privileged actions (user management, auth,
financial data, LC/contract lifecycle, hard deletes) - the AuditLog table
existed in the schema but had no writer anywhere in the codebase.

Mirrors infrastructure/activity/activity_service.py's pattern: the caller owns
the transaction (we only add() the row), and logging must never raise or break
the action it's recording.
"""

import logging
from models.database_models import AuditLog

logger = logging.getLogger("uvicorn")


def log_audit(db, user_id, action, entity_type=None, entity_id=None,
               old_value=None, new_value=None, description=None, ip_address=None):
    """Add an AuditLog row to the current session (committed by the caller).

    action      : e.g. "CREATE_USER", "UPDATE_USER", "RESET_PASSWORD", "LOGIN",
                  "LOGIN_FAILED", "LOGOUT", "CHANGE_PASSWORD", "DELETE", ...
    entity_type : e.g. "USER", "BANK_LIMIT", "CONTRACT", "LC", "CURRENCY_RATE"
    entity_id   : primary key of the affected row, if any
    old_value / new_value : optional JSON-serializable snapshots (dicts) for diffing
    description : freeform human-readable summary
    ip_address  : caller's IP, if available (e.g. request.client.host)
    """
    try:
        db.add(AuditLog(
            user_id=user_id,
            action=str(action)[:100],
            entity_type=(str(entity_type)[:50] if entity_type else None),
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            description=(str(description)[:2000] if description else None),
            ip_address=ip_address,
        ))
    except Exception as e:  # pragma: no cover - defensive; audit must never break the action
        logger.warning(f"log_audit failed (ignored): {e}")
