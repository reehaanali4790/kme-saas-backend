"""
AI Assistant API — ask natural-language questions, get DB-backed reports.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.tenant import get_tenant_db
from core.platform_metering import record_ai_usage_event
from config.settings import settings
from models.database_models import User
from modules.auth.dependencies import get_current_user
from modules.admin.assistant_service import ask
from modules.admin.assistant_schemas import AskRequest

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/assistant", tags=["AI Assistant"])


@router.post("/ask")
def ask_assistant(data: AskRequest, db: Session = Depends(get_tenant_db),
                  current_user: User = Depends(get_current_user)):
    question = (data.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not set up on this server. Please contact support, or enter the details manually.")
    try:
        result = ask(question, settings.ANTHROPIC_API_KEY)
        record_ai_usage_event(
            event_type="assistant",
            model="claude",
            doc_type=None,
            success=True,
        )
    except Exception as e:
        record_ai_usage_event(
            event_type="assistant",
            model="claude",
            doc_type=None,
            success=False,
        )
        logger.error(f"Assistant error: {e}")
        raise HTTPException(status_code=500, detail=f"Assistant failed: {str(e)}")
    return result
