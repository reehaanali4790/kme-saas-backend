"""
Migration: enforce uniqueness of the EB GD Number within one Into-Bond GD.

Prevents the same physical Ex-Bond document being recorded as two separate Partial GD
releases against the same Into-Bond GD (which would double-count against the bonded
quantity). Comparison is case/whitespace-insensitive.

Scoped to is_finalized = true: a gd_number is free to sit on any number of DRAFT
Partial GDs (e.g. two releases whose EB GD Views both got extracted before either is
validated) — this constraint only bites at the point a release is actually RECORDED
(validate-approval flips is_finalized to true), matching the business rule. It is NOT
meant to block the GD-View upload/extract step itself, only finalization.

This is a defense-in-depth guarantee alongside the app-level check in
partial_gd_service.validate_partial_gd_approval — the app-level check gives a friendly
error in the common case (and is intentionally slightly stricter, blocking against an
unfinalized duplicate draft too), while this index guarantees correctness even under a
race between two concurrent requests finalizing the same number at once.

Run once:  python backend/scripts/migrations/migrate_partial_gd_unique_gd_number.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text


def run_migration():
    print("EB GD Number uniqueness migration ...")
    with engine.begin() as conn:
        conn.execute(text(
            "DROP INDEX IF EXISTS ux_ex_bond_entries_ib_gd_number"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ex_bond_entries_ib_gd_number "
            "ON ex_bond_entries (into_bond_gd_id, UPPER(BTRIM(gd_number))) "
            "WHERE gd_number IS NOT NULL AND is_finalized = true"
        ))
        print("  ux_ex_bond_entries_ib_gd_number (scoped to is_finalized = true)")
    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
