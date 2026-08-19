"""Importer-desk gap close: LC amendments + container events.

Revision ID: 003_importer_desk_gaps
Revises: 002_exception_paths
Create Date: 2026-08-19

New tables only (idempotent CREATE TABLE IF NOT EXISTS). Safe on every deploy.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from infrastructure.migrations.helpers import run_sql_on_tenant_schemas

revision: str = "003_importer_desk_gaps"
down_revision: Union[str, None] = "002_exception_paths"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DDL = """
CREATE TABLE IF NOT EXISTS lc_amendments (
    amendment_id SERIAL PRIMARY KEY,
    lc_id INTEGER NOT NULL REFERENCES lc_master(lc_id) ON DELETE CASCADE,
    field_name VARCHAR(80) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by INTEGER,
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_lc_amendments_lc_id ON lc_amendments (lc_id);
CREATE INDEX IF NOT EXISTS ix_lc_amendments_created_at ON lc_amendments (created_at);

CREATE TABLE IF NOT EXISTS container_events (
    event_id SERIAL PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(shipment_id) ON DELETE CASCADE,
    bl_id INTEGER REFERENCES bill_of_ladings(bl_id) ON DELETE SET NULL,
    container_number VARCHAR(50) NOT NULL,
    event_type VARCHAR(20) NOT NULL,
    event_date DATE,
    last_free_date DATE,
    notes TEXT,
    source VARCHAR(20) DEFAULT 'MANUAL',
    created_at TIMESTAMP DEFAULT now(),
    created_by INTEGER,
    CONSTRAINT valid_container_event_type CHECK (
        event_type IN ('LOADED','DISCHARGED','AVAILABLE','GATE_OUT','EMPTY_RETURN')
    ),
    CONSTRAINT valid_container_event_source CHECK (
        source IS NULL OR source IN ('MANUAL','API')
    )
);
CREATE INDEX IF NOT EXISTS ix_container_events_shipment_id ON container_events (shipment_id);
CREATE INDEX IF NOT EXISTS ix_container_events_container_number ON container_events (container_number);
CREATE INDEX IF NOT EXISTS ix_container_events_last_free_date ON container_events (last_free_date);
CREATE INDEX IF NOT EXISTS idx_container_event_ship_cntr ON container_events (shipment_id, container_number);
"""


def upgrade() -> None:
    print("Alembic 003: importer-desk tables ...")
    conn = op.get_bind()
    run_sql_on_tenant_schemas(conn, _DDL, label="003_importer_desk_gaps")


def downgrade() -> None:
    conn = op.get_bind()
    run_sql_on_tenant_schemas(
        conn,
        "DROP TABLE IF EXISTS container_events; DROP TABLE IF EXISTS lc_amendments;",
        label="003_importer_desk_gaps_down",
    )
