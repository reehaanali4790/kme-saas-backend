"""
Migration: shipment ETD, insurance gross premium, record-keeping documents + activity log.

- shipments.etd (DATE)                          -> estimated time of departure
- insurance_certificates.gross_premium (NUMERIC)-> gross premium (for Premium Rate %)
- shipment_documents table                       -> DPL + free-form 'other' docs (no extraction)
- activity_logs table                            -> user activity / document trail

Additive / idempotent — safe for existing rows. Run once:
    python backend/migrate_shipment_docs_activity.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    with engine.begin() as conn:
        print("Adding shipments.etd ...")
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS etd DATE"))

        print("Adding insurance_certificates.gross_premium ...")
        conn.execute(text("ALTER TABLE insurance_certificates "
                          "ADD COLUMN IF NOT EXISTS gross_premium NUMERIC(15,2)"))

        print("Creating shipment_documents ...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shipment_documents (
                doc_id             SERIAL PRIMARY KEY,
                shipment_id        INTEGER NOT NULL REFERENCES shipments(shipment_id) ON DELETE CASCADE,
                doc_kind           VARCHAR(20) NOT NULL DEFAULT 'OTHER',
                doc_name           VARCHAR(200),
                document_filename  VARCHAR(255),
                document_path      TEXT,
                uploaded_at        TIMESTAMP DEFAULT now(),
                uploaded_by        INTEGER REFERENCES users(user_id),
                updated_at         TIMESTAMP DEFAULT now(),
                updated_by         INTEGER REFERENCES users(user_id),
                CONSTRAINT valid_shipment_doc_kind CHECK (doc_kind IN ('DPL','OTHER'))
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shipment_documents_shipment_id "
                          "ON shipment_documents(shipment_id)"))

        print("Creating activity_logs ...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                log_id       SERIAL PRIMARY KEY,
                shipment_id  INTEGER REFERENCES shipments(shipment_id) ON DELETE CASCADE,
                user_id      INTEGER REFERENCES users(user_id),
                action       VARCHAR(30) NOT NULL,
                doc_type     VARCHAR(80),
                detail       VARCHAR(500),
                created_at   TIMESTAMP DEFAULT now()
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_activity_logs_shipment_id "
                          "ON activity_logs(shipment_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_activity_logs_created_at "
                          "ON activity_logs(created_at)"))

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
