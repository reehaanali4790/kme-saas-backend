"""
Migration: WeBOC GD View / Item Details + Into-Bond–Ex-Bond tracking + SRO quota rework.

Adds (all ADDITIVE / idempotent — existing rows are untouched):

goods_declarations
  eta                     - ETA read off the GD View (drives the 18-day filing deadline and
                            the 180-day into-bond deadline)
  gd_view_uploaded        - GD View document uploaded + extracted
  item_details_uploaded   - Item Details document uploaded + extracted
  declaration_type_raw    - declaration type exactly as printed (IB / HC / EX / other)
  section82_penalty_pkr   - Section 82 / Penalty-F amount found in the charges block
  late_filed              - GD was filed late (Section 82 penalty present, or filed after ETA+18)
  bonded_qty_mt           - INTO-BOND only: total quantity entered into bond
  charges_breakdown       - JSONB list of every charge/penalty line on the GD View
  gd_type CHECK widened to allow 'UNKNOWN' (declaration type could not be determined)

gd_items
  declared_quantity / assessed_quantity  - Item Details declared vs assessed qty
  unit                                   - the unit those quantities are in
  quota_reference                        - EDB / SRO / quota approval reference on the item

ex_bond_entries (new)
  Ex-Bond GDs lifted against one Into-Bond GD (quantity lifted per entry).

edb_approvals (the SRO entry)
  company_name, main_sro_no, start_date, end_date, short_description

sro_group_numbers (new)
  Group / child SRO numbers under one Main SRO number (many per approval).

Run once:  python backend/migrate_weboc_sro.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.database import engine
from sqlalchemy import text

GD_COLS = [
    ("eta", "DATE"),
    ("gd_view_uploaded", "BOOLEAN DEFAULT FALSE"),
    ("item_details_uploaded", "BOOLEAN DEFAULT FALSE"),
    ("declaration_type_raw", "VARCHAR(30)"),
    ("section82_penalty_pkr", "NUMERIC(15,2)"),
    ("late_filed", "BOOLEAN DEFAULT FALSE"),
    ("bonded_qty_mt", "NUMERIC(12,3)"),
    ("charges_breakdown", "JSONB"),
]

GD_ITEM_COLS = [
    ("declared_quantity", "NUMERIC(15,3)"),
    ("assessed_quantity", "NUMERIC(15,3)"),
    ("unit", "VARCHAR(20)"),
    ("quota_reference", "TEXT"),
]

APPROVAL_COLS = [
    ("company_name", "VARCHAR(300)"),
    ("main_sro_no", "VARCHAR(50)"),
    ("start_date", "DATE"),
    ("end_date", "DATE"),
    ("short_description", "TEXT"),
]

EX_BOND_TABLE = """
CREATE TABLE IF NOT EXISTS ex_bond_entries (
    entry_id          SERIAL PRIMARY KEY,
    into_bond_gd_id   INTEGER NOT NULL REFERENCES goods_declarations(gd_id) ON DELETE CASCADE,
    shipment_id       INTEGER REFERENCES shipments(shipment_id) ON DELETE SET NULL,
    ex_bond_gd_id     INTEGER REFERENCES goods_declarations(gd_id) ON DELETE SET NULL,
    gd_number         VARCHAR(50),
    ex_bond_date      DATE,
    quantity_mt       NUMERIC(12,3),
    remarks           TEXT,
    created_at        TIMESTAMP DEFAULT NOW(),
    created_by        INTEGER REFERENCES users(user_id)
)
"""

SRO_GROUP_TABLE = """
CREATE TABLE IF NOT EXISTS sro_group_numbers (
    group_id      SERIAL PRIMARY KEY,
    approval_id   INTEGER NOT NULL REFERENCES edb_approvals(approval_id) ON DELETE CASCADE,
    group_sro_no  VARCHAR(50) NOT NULL,
    hs_code       VARCHAR(50),
    created_at    TIMESTAMP DEFAULT NOW()
)
"""


def _add_cols(conn, table, cols):
    for name, ddl in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        print(f"  {table}.{name}")


def run_migration():
    with engine.begin() as conn:
        print("Goods declarations — GD View / penalty / bond columns ...")
        _add_cols(conn, "goods_declarations", GD_COLS)

        # Declaration type may be undeterminable -> UNKNOWN (flagged for review in the UI).
        print("\nWidening valid_gd_type to allow UNKNOWN ...")
        conn.execute(text("ALTER TABLE goods_declarations DROP CONSTRAINT IF EXISTS valid_gd_type"))
        conn.execute(text(
            "ALTER TABLE goods_declarations ADD CONSTRAINT valid_gd_type "
            "CHECK (gd_type IN ('HOME_CONSUMPTION','INTO_BOND','EX_BOND','UNKNOWN'))"))
        print("  valid_gd_type updated")

        print("\nGD items — Item Details columns ...")
        _add_cols(conn, "gd_items", GD_ITEM_COLS)

        print("\nEx-bond entries table ...")
        conn.execute(text(EX_BOND_TABLE))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ex_bond_into_gd "
                          "ON ex_bond_entries(into_bond_gd_id)"))
        print("  ex_bond_entries")

        print("\nEDB / SRO approvals — company + main SRO + validity ...")
        _add_cols(conn, "edb_approvals", APPROVAL_COLS)

        print("\nSRO group (child) numbers table ...")
        conn.execute(text(SRO_GROUP_TABLE))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sro_group_approval "
                          "ON sro_group_numbers(approval_id)"))
        print("  sro_group_numbers")

        # Existing approvals were keyed on approval_no (e.g. V-319). The SRO form now leads with
        # the Main SRO Number — seed it from approval_no so old rows keep working and still show
        # in the SRO Quota Report. Raw SQL (no ORM) so this stays safe on older schemas.
        res = conn.execute(text(
            "UPDATE edb_approvals SET main_sro_no = approval_no "
            "WHERE main_sro_no IS NULL AND approval_no IS NOT NULL"))
        print(f"\n  back-filled main_sro_no on {res.rowcount} existing approval(s)")

    print("\nMigration complete.")


if __name__ == "__main__":
    run_migration()
