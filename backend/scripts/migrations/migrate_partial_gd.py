"""
Migration: Partial GD (Ex-Bond Release) rework.

Each Partial GD now carries its OWN EB GD View and its own (multiple) Item Detail
documents, scoped to one ex_bond_entries row, instead of a single generic document.
SRO numbers are extracted from these item details and validated against a
user-selected Quota Approval (edb_approvals) before the release can be recorded.

Adds (all ADDITIVE / idempotent — existing rows are untouched):

ex_bond_entries
  approval_id     - the Quota Approval (edb_approvals) this release was validated against
  matched_sro_no  - the SRO/quota reference that was validated against the approval
  is_finalized    - true once approval validation + save has succeeded

gd_attachments
  ex_bond_entry_id  - scopes a GD_VIEW/ITEM_DETAILS-style attachment to one Partial GD
                      (NULL for every existing attachment — unaffected)
  kind CHECK widened to allow EX_BOND_GD_VIEW / EX_BOND_ITEM_DETAILS

ex_bond_items (new)
  Item lines extracted from a Partial GD's own Item Details document(s) — separate from
  gd_items (which belongs to the IB's own Item Details) so SRO extraction for a release
  never reads from the IB's item details.

Run once:  python backend/scripts/migrations/migrate_partial_gd.py
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

EX_BOND_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS ex_bond_items (
    item_id               SERIAL PRIMARY KEY,
    entry_id              INTEGER NOT NULL REFERENCES ex_bond_entries(entry_id) ON DELETE CASCADE,
    source_attachment_id  INTEGER REFERENCES gd_attachments(attachment_id) ON DELETE SET NULL,
    item_number           INTEGER,
    hs_code               VARCHAR(50),
    goods_description     TEXT,
    quantity              NUMERIC(15,3),
    unit                  VARCHAR(20),
    declared_quantity     NUMERIC(15,3),
    assessed_quantity     NUMERIC(15,3),
    country_of_origin     VARCHAR(100),
    sro_no                TEXT,
    quota_reference       TEXT,
    unit_value_declared        NUMERIC(15,4),
    unit_value_assessed        NUMERIC(15,4),
    total_value_declared_usd   NUMERIC(18,2),
    total_value_assessed_usd   NUMERIC(18,2),
    custom_value_declared_pkr  NUMERIC(18,2),
    custom_value_assessed_pkr  NUMERIC(18,2),
    created_at            TIMESTAMP DEFAULT NOW()
)
"""


def run_migration():
    print("Partial GD (Ex-Bond Release) rework migration ...")
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE ex_bond_entries "
            "ADD COLUMN IF NOT EXISTS approval_id INTEGER "
            "REFERENCES edb_approvals(approval_id) ON DELETE SET NULL"
        ))
        print("  ex_bond_entries.approval_id")

        conn.execute(text(
            "ALTER TABLE ex_bond_entries ADD COLUMN IF NOT EXISTS matched_sro_no TEXT"
        ))
        print("  ex_bond_entries.matched_sro_no")

        conn.execute(text(
            "ALTER TABLE ex_bond_entries "
            "ADD COLUMN IF NOT EXISTS is_finalized BOOLEAN DEFAULT FALSE"
        ))
        print("  ex_bond_entries.is_finalized")

        conn.execute(text(
            "ALTER TABLE gd_attachments "
            "ADD COLUMN IF NOT EXISTS ex_bond_entry_id INTEGER "
            "REFERENCES ex_bond_entries(entry_id) ON DELETE CASCADE"
        ))
        print("  gd_attachments.ex_bond_entry_id")
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_gd_attachments_ex_bond_entry "
            "ON gd_attachments(ex_bond_entry_id)"
        ))

        conn.execute(text(
            "ALTER TABLE gd_attachments DROP CONSTRAINT IF EXISTS valid_gd_attachment_kind"
        ))
        conn.execute(text(
            f"ALTER TABLE gd_attachments ADD CONSTRAINT valid_gd_attachment_kind "
            f"CHECK (kind IN ({KINDS}))"
        ))
        print("  valid_gd_attachment_kind widened (EX_BOND_GD_VIEW, EX_BOND_ITEM_DETAILS)")

        print("\nEx-bond items table ...")
        conn.execute(text(EX_BOND_ITEMS_TABLE))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ex_bond_items_entry ON ex_bond_items(entry_id)"
        ))
        print("  ex_bond_items")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
