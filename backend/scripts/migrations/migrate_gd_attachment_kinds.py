"""
Migration: widen gd_attachments.valid_gd_attachment_kind CHECK constraint.

The app added new attachment kinds (ASSESSMENT, GD_VIEW, ITEM_DETAILS) but the DB
constraint still only allowed EXAMINATION/LAB, so uploading a GD View / Item Details
document raised a CheckViolation. This drops and re-adds the constraint with the full set.

Additive / idempotent — safe for existing rows. Run once:
    python backend/migrate_gd_attachment_kinds.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

# Must stay a SUPERSET of every kind the app writes: deploy_migrate.py re-runs this on
# every release, so narrowing it here breaks the release the moment a row exists with a
# kind added by a later migration (FINAL_GD, then INTO_BOND_GD / EX_BOND_GD, then
# EX_BOND_GD_VIEW / EX_BOND_ITEM_DETAILS — see migrate_partial_gd.py).
# Add new kinds here as well as in the migration that introduces them.
KINDS = ("'EXAMINATION','LAB','ASSESSMENT','GD_VIEW','ITEM_DETAILS','FINAL_GD',"
         "'INTO_BOND_GD','EX_BOND_GD','EX_BOND_GD_VIEW','EX_BOND_ITEM_DETAILS'")


def run_migration():
    print("Widening valid_gd_attachment_kind ...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE gd_attachments DROP CONSTRAINT IF EXISTS valid_gd_attachment_kind"))
        conn.execute(text(
            f"ALTER TABLE gd_attachments ADD CONSTRAINT valid_gd_attachment_kind "
            f"CHECK (kind IN ({KINDS}))"
        ))
        print("  valid_gd_attachment_kind updated")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
