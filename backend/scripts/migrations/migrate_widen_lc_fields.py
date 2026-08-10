"""
Migration: widen the lc_master / lc_products text columns that real LC wording overflows.

Root cause: creating an LC from a SWIFT MT700 crashed with
    psycopg2.errors.StringDataRightTruncation: value too long for type character varying(50)
on INSERT into lc_master. The extracted LC fields carry full document wording, e.g.
delivery_terms = "CFR KARACHI PORT, PAKISTAN AS PER INCOTERMS 2020" (47) or longer with a
qualifier, and payment_terms/drafts-at clauses run to whole sentences. The old widths were
sized for hand-typed codes ("CFR"), not for extracted clauses.

Fix: clause-like fields become TEXT (unbounded — no business limit, must never truncate);
identifier/name fields are widened to lengths matching the equivalent columns on
contracts/commercial_invoices (300 for party names, 100 for identifiers).

Additive / idempotent and NON-DESTRUCTIVE: every change only WIDENS a column (or converts
it to TEXT). Postgres does these without a table rewrite and without touching existing
rows, so no data is lost or altered. A column already at/above the target is skipped, so
this is safe to re-run.

Run once (safe to re-run):
    python backend/migrate_widen_lc_fields.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine
from sqlalchemy import text

# (table, column, target_type) — target_type is "TEXT" or "VARCHAR(n)".
# Only applied when it is strictly wider than what's already there.
COLUMNS = [
    # --- lc_master: free-form LC wording -> TEXT (no real business limit) ---
    ("lc_master", "delivery_terms", "TEXT"),    # the column that was crashing (was 50)
    ("lc_master", "payment_terms", "TEXT"),     # SWIFT 42C "drafts at" clauses are sentences
    ("lc_master", "insurance", "TEXT"),         # insurer + policy wording

    # --- lc_master: identifiers / names -> widened, still bounded ---
    ("lc_master", "lc_number", "VARCHAR(100)"),
    ("lc_master", "contract_number", "VARCHAR(100)"),
    ("lc_master", "supplier_name", "VARCHAR(300)"),   # matches contracts.supplier_name
    ("lc_master", "importer_name", "VARCHAR(300)"),   # matches contracts.buyer_name
    ("lc_master", "indentor", "VARCHAR(300)"),
    ("lc_master", "bank_name", "VARCHAR(300)"),
    ("lc_master", "booked_by", "VARCHAR(200)"),
    ("lc_master", "hoa", "VARCHAR(200)"),
    ("lc_master", "arrival_port", "VARCHAR(200)"),
    ("lc_master", "vessel_name", "VARCHAR(300)"),
    ("lc_master", "currency", "VARCHAR(20)"),         # "USD", but docs print "US DOLLAR"
    ("lc_master", "reimbursement", "VARCHAR(50)"),

    # --- lc_products: goods description / origin come straight from the LC ---
    # goods description (SWIFT 45A) runs to several lines on a real LC -> TEXT (was 100)
    ("lc_products", "product_name", "TEXT"),
    ("lc_products", "product_code", "VARCHAR(50)"),
    ("lc_products", "origin", "VARCHAR(100)"),
    ("lc_products", "quality", "VARCHAR(50)"),
    ("lc_products", "cargo_nature", "VARCHAR(50)"),
    ("lc_products", "unit", "VARCHAR(20)"),
]


def _target_width(target: str) -> int:
    """Sort key for 'is this wider?' — TEXT is unbounded, treat as infinite."""
    if target.upper() == "TEXT":
        return 10 ** 9
    return int(target.split("(")[1].rstrip(")"))


def run_migration():
    widened, skipped, missing = 0, 0, 0

    with engine.begin() as conn:
        for table, column, target in COLUMNS:
            row = conn.execute(text(
                "SELECT data_type, character_maximum_length FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {"t": table, "c": column}).first()

            if row is None:
                print(f"  SKIP {table}.{column} (column does not exist)")
                missing += 1
                continue

            data_type, current_len = row
            # TEXT has no character_maximum_length -> already unbounded, nothing to widen.
            current = 10 ** 9 if current_len is None else current_len
            if current >= _target_width(target):
                print(f"  ok   {table}.{column} already {data_type}"
                      f"{'' if current_len is None else f'({current_len})'}")
                skipped += 1
                continue

            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {target}"))
            print(f"  +    {table}.{column}: varchar({current_len}) -> {target}")
            widened += 1

    print(f"\nWidened {widened}, already-ok {skipped}, missing {missing}.")
    print("Migration complete. (Widening only — no existing row was modified.)")


if __name__ == "__main__":
    run_migration()
