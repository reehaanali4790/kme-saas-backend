"""
LME Monitoring System - Database Connection
Version: 2.0
"""

import logging
import os
import sys
import time
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings

PLATFORM_SCHEMA = "platform"
SHARED_SCHEMA = "shared"


def resolve_database_url() -> str:
    """Pick a Postgres URL that works in every Railway deploy phase."""
    internal = os.environ.get("DATABASE_URL")
    public = os.environ.get("DATABASE_PUBLIC_URL")
    url = internal or public or settings.DATABASE_URL

    if internal and ".railway.internal" in internal:
        if public:
            url = public
        else:
            logging.getLogger("uvicorn").warning(
                "DATABASE_URL uses postgres.railway.internal but DATABASE_PUBLIC_URL "
                "is not set — pre-deploy migrations may fail."
            )

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = resolve_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,
)

logger = logging.getLogger("uvicorn")
SLOW_QUERY_MS = int(os.environ.get("SLOW_QUERY_MS", "200"))


@event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("query_start_time", []).append(time.perf_counter())


@event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    start = conn.info["query_start_time"].pop(-1)
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms >= SLOW_QUERY_MS:
        logger.warning(
            "SLOW QUERY (%.1fms): %s",
            elapsed_ms, " ".join(statement.split())[:500],
        )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """Legacy dependency — platform + shared + public search path."""
    db = SessionLocal()
    try:
        db.execute(text(f"SET search_path TO {PLATFORM_SCHEMA}, {SHARED_SCHEMA}, public"))
        yield db
    finally:
        db.close()


def set_platform_search_path(db: Session) -> None:
    db.execute(text(f"SET search_path TO {PLATFORM_SCHEMA}, {SHARED_SCHEMA}"))


def get_platform_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        set_platform_search_path(db)
        yield db
    finally:
        db.close()


def set_tenant_search_path(db: Session, schema_name: str) -> None:
    db.execute(text(f"SET search_path TO {schema_name}, {SHARED_SCHEMA}, {PLATFORM_SCHEMA}"))


def init_db():
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully!")


def check_db_connection() -> bool:
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return True
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False
