"""Baseline — ensure platform/shared schemas exist.

Revision ID: 001_baseline
Revises:
Create Date: 2026-08-17

Existing databases that ran legacy deploy_migrate scripts are stamped here (or at head)
before ``alembic upgrade head`` runs. Fresh databases get schemas created before tenant
DDL in later revisions.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from config.database import PLATFORM_SCHEMA, SHARED_SCHEMA

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text(f"CREATE SCHEMA IF NOT EXISTS {PLATFORM_SCHEMA}"))
    op.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SHARED_SCHEMA}"))


def downgrade() -> None:
    pass
