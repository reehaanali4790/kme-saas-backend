"""
Runtime schema guard — applies critical additive DDL when the live DB is behind the code.

Railway/Heroku *should* run a migration step before the new image serves traffic, but if
that step is skipped or an older image starts before migrations finish, queries against
new ORM columns will 500. This module patches known gaps idempotently on startup so
production self-heals. Ported from rehan/newchanges (backend/schema_guard.py).
"""

import logging

from sqlalchemy import inspect, text

from config.database import SessionLocal, engine

logger = logging.getLogger("uvicorn")

# (table, column, ddl fragment after column name)
_COLUMN_PATCHES = [
    ("importers", "short_code", "VARCHAR(12)"),
    ("bill_of_ladings", "demurrage_total_amount", "NUMERIC(12, 2)"),
    ("bill_of_ladings", "detention_start_date", "DATE"),
    ("bill_of_ladings", "detention_free_days", "INTEGER"),
    ("bill_of_ladings", "detention_end_date", "DATE"),
    ("bill_of_ladings", "detention_total_amount", "NUMERIC(12, 2)"),
    ("bill_of_ladings", "detention_currency", "VARCHAR(10) DEFAULT 'USD'"),
    ("bill_of_ladings", "detention_paid_date", "DATE"),
    ("bill_of_ladings", "detention_remarks", "TEXT"),
    ("bill_of_ladings", "bl_type", "VARCHAR(20)"),
    ("shipments", "on_port_date", "DATE"),
    ("shipments", "departure_date", "DATE"),
    ("shipments", "kpt_berth", "VARCHAR(100)"),
    ("gd_kgtl_weighments", "own_coils", "INTEGER"),
    ("gd_kgtl_weighments", "kgtl_coils", "INTEGER"),
    ("lc_products", "mill_name", "VARCHAR(200)"),
    ("contract_items", "mill_name", "VARCHAR(200)"),
    ("lme_bulletins", "source", "VARCHAR(10) NOT NULL DEFAULT 'MANUAL'"),
    ("gd_items", "m_size", "VARCHAR(100)"),
    ("ex_bond_items", "m_size", "VARCHAR(100)"),
    ("goods_declarations", "lab_report_assessment", "BOOLEAN DEFAULT FALSE"),
    ("lme_bulletins", "whatsapp_rates_sent", "BOOLEAN NOT NULL DEFAULT false"),
    ("lme_bulletins", "whatsapp_rates_sent_at", "TIMESTAMP"),
]


def _table_exists(table: str) -> bool:
    return table in inspect(engine).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    cols = {c["name"] for c in inspect(engine).get_columns(table)}
    return column in cols


def _ensure_column(table: str, column: str, ddl: str) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"
            ))
        return True
    except Exception as exc:
        logger.warning(f"Schema guard patch {table}.{column} failed: {exc}")
        return False


def _ensure_importers_short_code_index() -> None:
    if not _column_exists("importers", "short_code"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_importers_short_code ON importers (short_code)"
        ))


def _seed_importers_if_needed() -> None:
    if not _column_exists("importers", "short_code"):
        return
    try:
        from infrastructure.normalization.normalization_service import seed_known_importers
        db = SessionLocal()
        try:
            seed_known_importers(db)
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("seed_known_importers after short_code patch failed: %s", exc)


def ensure_runtime_schema() -> list[str]:
    """Return list of patches applied this startup (empty when DB already up to date)."""
    applied = []
    for table, column, ddl in _COLUMN_PATCHES:
        if _ensure_column(table, column, ddl):
            applied.append(f"{table}.{column}")
    if any(p.startswith("importers.") for p in applied):
        _ensure_importers_short_code_index()
        _seed_importers_if_needed()
    if applied:
        logger.info("Runtime schema guard applied: %s", ", ".join(applied))
    try:
        from models.database_models import Base, ContainerEvent, LCAmendment
        Base.metadata.create_all(
            bind=engine,
            tables=[ContainerEvent.__table__, LCAmendment.__table__],
            checkfirst=True,
        )
    except Exception as exc:
        logger.warning("Schema guard create_all for importer-desk tables failed: %s", exc)
    return applied
