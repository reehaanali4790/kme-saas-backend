"""
Guardrail against varchar overflow on save.

Extracted document data is unbounded free text, but many columns are bounded VARCHARs.
When a value overruns its column, Postgres raises StringDataRightTruncation and the whole
INSERT dies — which is how LC create used to fail with a raw psycopg2 error on screen.

Columns that carry real LC/document *wording* are TEXT (see migrate_widen_lc_fields.py), so
they can never overflow. This module is the safety net for the remaining bounded columns:

  fit_values(Model, values, hard_fields=...)
    -> (clean_values, warnings)

  * a bounded field that overruns is clamped to its column width and REPORTED in `warnings`
    (never silently — the caller surfaces these to the user), and logged server-side with
    the field, the limit and the actual length.
  * a `hard_fields` entry (a real business identifier like lc_number, where a clipped value
    would be wrong rather than merely shortened) raises FieldTooLong instead, so the caller
    can show a clean validation message and let the user correct it.
"""

import logging
from sqlalchemy import String
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger("uvicorn")

_LIMIT_CACHE: dict[str, dict[str, int]] = {}


class FieldTooLong(Exception):
    """A business-critical field exceeded its column limit. `message` is user-safe."""

    def __init__(self, field: str, limit: int, actual: int, label: str = None):
        self.field = field
        self.limit = limit
        self.actual = actual
        name = label or field.replace("_", " ").title()
        self.message = (f"{name} is too long ({actual} characters). "
                        f"Please shorten it to {limit} characters or less.")
        super().__init__(self.message)


def column_limits(model) -> dict[str, int]:
    """{column_name: max_length} for every bounded String column on `model`.
    TEXT / non-string columns are absent (they have no limit)."""
    key = model.__tablename__
    if key not in _LIMIT_CACHE:
        limits = {}
        for col in sa_inspect(model).columns:
            if isinstance(col.type, String) and col.type.length:
                limits[col.key] = col.type.length
        _LIMIT_CACHE[key] = limits
    return _LIMIT_CACHE[key]


def fit_values(model, values: dict, hard_fields: set = None,
               labels: dict = None, context: str = "") -> tuple[dict, list[str]]:
    """Clamp over-long values to their column widths. Returns (clean_values, warnings).

    Raises FieldTooLong for any `hard_fields` entry that overruns.
    """
    hard_fields = hard_fields or set()
    labels = labels or {}
    limits = column_limits(model)
    clean, warnings = dict(values), []

    for field, value in values.items():
        limit = limits.get(field)
        if limit is None or value is None or not isinstance(value, str):
            continue
        if len(value) <= limit:
            continue

        label = labels.get(field, field.replace("_", " ").title())
        if field in hard_fields:
            logger.error(f"{context}field '{field}' rejected: {len(value)} chars exceeds "
                         f"limit {limit}. Value={value[:200]!r}")
            raise FieldTooLong(field, limit, len(value), label)

        clean[field] = value[:limit]
        warnings.append(f"{label} was shortened to fit ({len(value)} characters, "
                        f"limit {limit}). Please check it.")
        logger.warning(f"{context}field '{field}' clamped: {len(value)} chars -> {limit}. "
                       f"Full value={value[:500]!r}")

    return clean, warnings
