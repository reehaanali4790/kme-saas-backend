"""
Migration — Shipment status automation, soft-delete, remarks + GD/WebOC "Final GD".

Adds:
  1. shipments.remarks          TEXT      — manual remarks per shipment
  2. shipments.is_deleted       BOOLEAN   — soft-delete flag (default FALSE)
     shipments.deleted_at       TIMESTAMP
     shipments.deleted_by       INTEGER
  3. Widen shipments.valid_shipment_status CHECK to include the new automatic
     status codes (COPY_DOCS_RECEIVED / DOCS_AT_BANK / PAYMENT_RECEIVED /
     ORIGINAL_DOCS_RECEIVED) while KEEPING every legacy code so existing rows
     stay valid (no backfill — old rows re-derive when next edited/validated).
  4. Widen gd_attachments.valid_gd_attachment_kind to add 'FINAL_GD' (uploading a
     Final GD marks the GD Released).

All additive / idempotent. Run once (safe to re-run):
    python backend/migrate_shipment_status_gd_weboc.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

# New automatic codes + every legacy code (so we never invalidate existing rows).
SHIPMENT_STATUSES = (
    "'PENDING','COPY_DOCS_RECEIVED','DOCS_AT_BANK','PAYMENT_RECEIVED',"
    "'ORIGINAL_DOCS_RECEIVED','DELIVERED',"
    # legacy — kept valid for un-backfilled rows
    "'DOCUMENTS_RECEIVED','VALIDATED','DISCREPANT','BANK_PRESENTATION',"
    "'ACCEPTED','PAYMENT_MADE','CLOSED'"
)

# Superset of every kind the app writes — this re-runs on every deploy, so it must not
# narrow away kinds added later (INTO_BOND_GD / EX_BOND_GD, see migrate_ib_ex_bond_gd.py;
# EX_BOND_GD_VIEW / EX_BOND_ITEM_DETAILS, see migrate_partial_gd.py).
GD_ATTACHMENT_KINDS = ("'EXAMINATION','LAB','ASSESSMENT','GD_VIEW','ITEM_DETAILS','FINAL_GD',"
                       "'INTO_BOND_GD','EX_BOND_GD','EX_BOND_GD_VIEW','EX_BOND_ITEM_DETAILS'")


def run_migration():
    with engine.begin() as conn:
        print("1) shipments.remarks ...")
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS remarks TEXT"))
        print("   + remarks")

        print("\n2) shipments soft-delete columns ...")
        conn.execute(text(
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"))
        # ensure no NULLs then enforce NOT NULL
        conn.execute(text("UPDATE shipments SET is_deleted = FALSE WHERE is_deleted IS NULL"))
        conn.execute(text("ALTER TABLE shipments ALTER COLUMN is_deleted SET DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE shipments ALTER COLUMN is_deleted SET NOT NULL"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS deleted_by INTEGER"))
        print("   + is_deleted / deleted_at / deleted_by")

        print("\n3) shipments valid_shipment_status CHECK (widen) ...")
        conn.execute(text("ALTER TABLE shipments DROP CONSTRAINT IF EXISTS valid_shipment_status"))
        conn.execute(text(
            f"ALTER TABLE shipments ADD CONSTRAINT valid_shipment_status "
            f"CHECK (status IN ({SHIPMENT_STATUSES}))"))
        print("   constraint valid_shipment_status updated.")

        print("\n4) gd_attachments valid_gd_attachment_kind (add FINAL_GD) ...")
        conn.execute(text(
            "ALTER TABLE gd_attachments DROP CONSTRAINT IF EXISTS valid_gd_attachment_kind"))
        conn.execute(text(
            f"ALTER TABLE gd_attachments ADD CONSTRAINT valid_gd_attachment_kind "
            f"CHECK (kind IN ({GD_ATTACHMENT_KINDS}))"))
        print("   constraint valid_gd_attachment_kind updated.")

        print("\n5) goods_declarations.final_gd_uploaded ...")
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD COLUMN IF NOT EXISTS "
            "final_gd_uploaded BOOLEAN DEFAULT FALSE"))
        conn.execute(text(
            "UPDATE goods_declarations SET final_gd_uploaded = FALSE WHERE final_gd_uploaded IS NULL"))
        print("   + final_gd_uploaded")

    with engine.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='shipments' AND column_name IN "
            "('remarks','is_deleted','deleted_at','deleted_by')")).fetchall()
        print(f"\n  shipments new columns present: {sorted(c[0] for c in cols)}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
