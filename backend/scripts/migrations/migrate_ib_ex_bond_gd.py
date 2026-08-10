"""
Migration: Into-Bond / Ex-Bond GD document workflow.

- goods_declarations.into_bond_gd_uploaded
- ex_bond_entries.attachment_id -> gd_attachments
- gd_attachments kinds: INTO_BOND_GD, EX_BOND_GD

Run once: python backend/migrate_ib_ex_bond_gd.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

KINDS = (
    "'EXAMINATION','LAB','ASSESSMENT','GD_VIEW','ITEM_DETAILS','FINAL_GD',"
    "'INTO_BOND_GD','EX_BOND_GD','EX_BOND_GD_VIEW','EX_BOND_ITEM_DETAILS'"
)


def run_migration():
    print("IB / Ex-Bond GD document workflow migration ...")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE goods_declarations "
            "ADD COLUMN IF NOT EXISTS into_bond_gd_uploaded BOOLEAN DEFAULT FALSE"
        ))
        print("  goods_declarations.into_bond_gd_uploaded")

        conn.execute(text(
            "ALTER TABLE ex_bond_entries "
            "ADD COLUMN IF NOT EXISTS attachment_id INTEGER "
            "REFERENCES gd_attachments(attachment_id) ON DELETE SET NULL"
        ))
        print("  ex_bond_entries.attachment_id")

        conn.execute(text(
            "ALTER TABLE gd_attachments DROP CONSTRAINT IF EXISTS valid_gd_attachment_kind"
        ))
        conn.execute(text(
            f"ALTER TABLE gd_attachments ADD CONSTRAINT valid_gd_attachment_kind "
            f"CHECK (kind IN ({KINDS}))"
        ))
        print("  valid_gd_attachment_kind widened")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
