"""Business logic for the shared name-master lookup endpoint, extracted from
modules/reports/lookup_router.py as part of the Phase 4 module rollout.
"""
import re

from sqlalchemy.orm import Session

from core.exceptions import NotFoundError
from models.database_models import Importer, Supplier, Indentor, BookedBy, Bank
from infrastructure.normalization.normalization_service import match_bank, match_company, company_resolver
from modules.reports.lookup_schemas import LookupCreate

KINDS = {"importer": Importer, "supplier": Supplier, "indentor": Indentor,
         "booked_by": BookedBy, "bank": Bank}


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def model_for(kind: str):
    M = KINDS.get(kind)
    if not M:
        raise NotFoundError(f"Unknown lookup '{kind}'. Use one of: {', '.join(KINDS)}")
    return M


def search(db: Session, kind: str, q: str) -> list:
    import orjson
    from core.redis import redis_cache

    from core.cache_keys import lookup_key

    query_param = norm(q) if q else "default"
    cache_key = lookup_key(kind, query_param)

    # Try reading from cache
    cached_data = redis_cache.get(cache_key)
    if cached_data:
        try:
            return orjson.loads(cached_data)
        except Exception:
            pass

    # Cache miss
    M = model_for(kind)
    query = db.query(M)
    if q and q.strip():
        query = query.filter(M.name_norm.contains(norm(q)))
    rows = query.order_by(M.name).limit(50).all()
    if kind == "importer":
        cr = company_resolver(db)
        results = [{"id": r.id, "name": r.name,
                    "short_code": r.short_code or cr.resolve(r.name).get("short_code")}
                   for r in rows]
    else:
        results = [{"id": r.id, "name": r.name} for r in rows]

    # Cache result for 7 days
    try:
        redis_cache.set(cache_key, orjson.dumps(results).decode("utf-8"), ex=604800)
    except Exception:
        pass

    return results


def add(db: Session, kind: str, data: LookupCreate, user_id: int) -> dict:
    """Raises ValueError (translated to HTTPException(400) by the router) for a blank
    name - matches the original's HTTPException(400, "name is required").

    Goes through match_company/match_bank (fuzzy auto-merge + needs_review flagging) rather
    than a plain name_norm dedup, so "Meen Enterprises" and "Meen Enterprises Pvt Ltd"
    typed here land on the same master row instead of creating a near-duplicate."""
    from core.redis import redis_cache

    M = model_for(kind)
    name = str(data.name or "").strip()
    if not name:
        raise ValueError("name is required")

    if kind == "bank":
        canon, row, review = match_bank(name, db)
        if not row:
            raise ValueError("Could not normalize bank name")
    else:
        canon, row, review = match_company(name, db, M)
        if not row:
            raise ValueError("Could not normalize name")
    if review and user_id:
        row.created_by = user_id
    db.commit()
    db.refresh(row)

    out = {"id": row.id, "name": row.name, "created": bool(review)}
    if kind == "importer" and hasattr(row, "short_code"):
        out["short_code"] = row.short_code or company_resolver(db).resolve(row.name).get("short_code")

    # Invalidate cache for this lookup kind
    try:
        from core.cache_keys import lookup_pattern
        redis_cache.delete_pattern(lookup_pattern(kind))
    except Exception:
        pass

    return out
