"""
Migration: GD enhancements — examination report, lab report, gd_type (into/ex-bond),
extended status workflow, and SRO/EDB quota tracking.

- Add columns to goods_declarations (gd_type, into_bond link, exam fields, lab fields).
- Widen the goods_declarations status CheckConstraint and add a gd_type constraint.
- Create gd_attachments, edb_approvals, sro_quota_items tables.

All additive — safe for existing rows. Run once (idempotent — safe to re-run):
    python backend/migrate_gd_enhancements.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import GDAttachment, EdbApproval, SroQuotaItem
from sqlalchemy import text

GD_COLUMNS = [
    ("gd_type", "VARCHAR(20) DEFAULT 'HOME_CONSUMPTION'"),
    ("into_bond_gd_id", "INTEGER REFERENCES goods_declarations(gd_id) ON DELETE SET NULL"),
    ("examination_report", "TEXT"),
    ("vir_no", "VARCHAR(100)"),
    ("index_no", "VARCHAR(100)"),
    ("examined_date", "DATE"),
    ("lab_report_available", "BOOLEAN DEFAULT FALSE"),
    ("lab_officer", "VARCHAR(200)"),
    ("lab_report", "TEXT"),
]

# Superset of every status this table has ever held across the migration chain.
# Includes the legacy PAID/OUT_OF_CHARGE (present before migrate_gd_status_and_vessel_loc
# runs) AND the newer RELEASED (present after it runs). Keeping both makes this constraint
# safe to (re-)apply regardless of whether the status-rename migration has run yet — the
# later migrate_gd_status_and_vessel_loc tightens it to the final set.
STATUS_VALUES = ("'DRAFT','FILED','EXAMINATION','ASSESSED','PAID','OUT_OF_CHARGE',"
                 "'RELEASED','INTO_BOND','CLEARED','DISPUTED'")


def run_migration():
    print("Adding GD enhancement columns to goods_declarations...")
    with engine.begin() as conn:
        for name, ddl in GD_COLUMNS:
            conn.execute(text(
                f"ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS {name} {ddl}"
            ))
            print(f"  + {name}")

    print("\nWidening status constraint + adding gd_type constraint...")
    with engine.begin() as conn:
        # Status: widen to include the new workflow stages.
        conn.execute(text("ALTER TABLE goods_declarations DROP CONSTRAINT IF EXISTS valid_gd_status"))
        conn.execute(text(
            f"ALTER TABLE goods_declarations ADD CONSTRAINT valid_gd_status "
            f"CHECK (status IN ({STATUS_VALUES}))"
        ))
        print("  valid_gd_status updated")
        # gd_type constraint (drop-first so re-runs don't error). Kept a superset with
        # migrate_weboc_sro.py's later widening (adds UNKNOWN) so this doesn't narrow the
        # constraint back below a value real rows may already carry — see the
        # gd_attachment_kind incident this mirrors.
        conn.execute(text("ALTER TABLE goods_declarations DROP CONSTRAINT IF EXISTS valid_gd_type"))
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD CONSTRAINT valid_gd_type "
            "CHECK (gd_type IN ('HOME_CONSUMPTION','INTO_BOND','EX_BOND','UNKNOWN'))"
        ))
        print("  valid_gd_type added")

    print("\nCreating gd_attachments, edb_approvals, sro_quota_items tables...")
    Base.metadata.create_all(
        bind=engine,
        tables=[GDAttachment.__table__, EdbApproval.__table__, SroQuotaItem.__table__],
    )
    print("  tables ready.")

    # Verify
    with engine.connect() as conn:
        gd_cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'goods_declarations'
              AND column_name IN ('gd_type','into_bond_gd_id','examination_report','vir_no',
                                  'index_no','examined_date','lab_report_available',
                                  'lab_officer','lab_report')
        """)).fetchall()
        tables = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('gd_attachments','edb_approvals','sro_quota_items')
        """)).fetchall()
        print(f"\n  goods_declarations new cols: {sorted({c[0] for c in gd_cols})}")
        print(f"  new tables: {sorted({t[0] for t in tables})}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
