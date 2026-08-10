"""
Activity / audit trail helper. One tiny function that endpoints call to record who did
what to a shipment (document uploads/replaces/edits, status & detail changes).

Kept deliberately simple: the caller owns the transaction, so we only add() the row and
let the caller's existing commit persist it. Never raises — logging must not break the
main action.
"""

import logging
from models.database_models import ActivityLog

logger = logging.getLogger("uvicorn")


def log_activity(db, shipment_id, user_id, action, doc_type=None, detail=None):
    """Add an ActivityLog row to the current session (committed by the caller).

    action   : UPLOAD / REPLACE / EDIT / STATUS / UPDATE / DELETE
    doc_type : e.g. "Bill of Lading", "DPL", "Delivery Order" (optional)
    detail   : freeform, e.g. "status Pending -> Delivered" (optional)
    """
    try:
        db.add(ActivityLog(
            shipment_id=shipment_id,
            user_id=user_id,
            action=action,
            doc_type=(str(doc_type)[:80] if doc_type else None),
            detail=(str(detail)[:500] if detail else None),
        ))
    except Exception as e:  # pragma: no cover - defensive; audit must never break the action
        logger.warning(f"log_activity failed (ignored): {e}")
