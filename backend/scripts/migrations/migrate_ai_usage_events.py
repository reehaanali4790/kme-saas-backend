"""Create platform.ai_usage_events for SaaS Admin Suite metering."""

from sqlalchemy import text

from config.database import SessionLocal


def migrate():
    db = SessionLocal()
    try:
        db.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        db.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS platform.ai_usage_events (
                    event_id SERIAL PRIMARY KEY,
                    organization_id INTEGER REFERENCES platform.organizations(organization_id) ON DELETE SET NULL,
                    event_type VARCHAR(50) NOT NULL DEFAULT 'extraction',
                    model VARCHAR(100),
                    doc_type VARCHAR(50),
                    success BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                )
                """
            )
        )
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ai_usage_org_created
                ON platform.ai_usage_events (organization_id, created_at)
                """
            )
        )
        db.commit()
        print("OK platform.ai_usage_events")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
