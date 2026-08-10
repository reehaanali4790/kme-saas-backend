"""
Migration: Normalization batch (Jul 2026).

Adds the schema + backfills for:
  1. needs_review flag on the name-master tables (importers/suppliers/indentors/booked_by).
  2. banks master table (+ seed from known bank canon names and existing lc_master.bank_name).
  3. lc_products.item_code / item_review + contract_items.item_code / item_review
     (normalized short product codes).
  4. shipments manual milestone fields (payment_date, original_doc_date, retirement_date,
     intimation_date, maturity_date, exchange_rate) + extend status CHECK to allow 'CLOSED'.
  5. contracts.bank_name + contracts.sent_to_bank_at (bank timeline tracking; backfill
     sent_to_bank_at = created_at).
  6. Seed company masters with the clean canonical names, then OVERWRITE existing company
     names (lc_master, contracts, goods_declarations) with their canonical values and set
     lc_products/contract_items.item_code from the item description. Originals are copied to
     a normalization_backup table first (reversible / backup-friendly).

Additive + idempotent (ADD COLUMN IF NOT EXISTS, re-runnable backfill). Run once:
    python backend/migrate_normalization_batch.py
"""

import sys
import os
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.database import engine, Base
from models.database_models import (
    Importer, Supplier, Indentor, BookedBy, Bank,
    LCMaster, LCProduct, Contract, ContractItem, GoodsDeclaration,
)
from infrastructure.normalization.normalization_service import (
    match_company, normalize_item, norm_bank, company_key, VALID_ITEM_CODES,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

# Clean canonical company names to seed (the "suggested clean company names").
SEED_COMPANIES = [
    "PERFECT CRAFT (SMC-PVT) LTD",
    "MAX COMFORT (PVT) LTD",
    "MEEN ENTERPRISES (SMC-PVT) LTD",
    "RANGE INDUSTRIES (SMC-PVT) LTD",
    "STEEL CRAFT (PVT) LTD",
]

# Bank canon names to seed (mirrors normalization_service._BANK_RULES targets).
SEED_BANKS = [
    "MCB Islamic Bank", "MCB Bank", "Habib Metro Bank", "Bank Al Habib", "HBL",
    "Soneri Bank", "BankIslami", "UBL", "Al Baraka Bank", "Askari Bank", "Meezan Bank",
    "Bank Alfalah", "Allied Bank", "National Bank of Pakistan", "Faysal Bank",
    "Standard Chartered", "Dubai Islamic Bank", "JS Bank", "Sindh Bank", "Bank of Punjab",
    "Bank of Khyber", "Summit Bank", "Silkbank", "Samba Bank", "Citibank",
    "Deutsche Bank", "ICBC",
]


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def _add_columns():
    with engine.begin() as conn:
        print("1) name-master needs_review flags...")
        for t in ("importers", "suppliers", "indentors", "booked_by"):
            conn.execute(text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS needs_review BOOLEAN DEFAULT FALSE"))
        print("   done.")

        print("2) banks master table...")
        Base.metadata.create_all(bind=engine, tables=[Bank.__table__])
        print("   done.")

        print("3) lc_products / contract_items item_code...")
        conn.execute(text("ALTER TABLE lc_products ADD COLUMN IF NOT EXISTS item_code VARCHAR(20)"))
        conn.execute(text("ALTER TABLE lc_products ADD COLUMN IF NOT EXISTS item_review BOOLEAN DEFAULT FALSE"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_lc_products_item_code ON lc_products (item_code)"))
        conn.execute(text("ALTER TABLE contract_items ADD COLUMN IF NOT EXISTS item_code VARCHAR(20)"))
        conn.execute(text("ALTER TABLE contract_items ADD COLUMN IF NOT EXISTS item_review BOOLEAN DEFAULT FALSE"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_contract_items_item_code ON contract_items (item_code)"))
        print("   done.")

        print("4) shipments manual fields + CLOSED status...")
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS payment_date DATE"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS original_doc_date DATE"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS retirement_date DATE"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS intimation_date DATE"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS maturity_date DATE"))
        conn.execute(text("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS exchange_rate NUMERIC(12,4)"))
        # Full superset (new automatic codes + legacy) — kept identical to the other
        # migrations that rebuild this constraint so repeat deploys never reject a row.
        conn.execute(text("ALTER TABLE shipments DROP CONSTRAINT IF EXISTS valid_shipment_status"))
        conn.execute(text(
            "ALTER TABLE shipments ADD CONSTRAINT valid_shipment_status CHECK (status IN "
            "('PENDING','COPY_DOCS_RECEIVED','DOCS_AT_BANK','PAYMENT_RECEIVED',"
            "'ORIGINAL_DOCS_RECEIVED','DELIVERED',"
            "'DOCUMENTS_RECEIVED','VALIDATED','DISCREPANT','BANK_PRESENTATION',"
            "'ACCEPTED','PAYMENT_MADE','CLOSED'))"))
        print("   done.")

        print("5) contracts bank timeline fields...")
        conn.execute(text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS bank_name VARCHAR(200)"))
        conn.execute(text("ALTER TABLE contracts ADD COLUMN IF NOT EXISTS sent_to_bank_at TIMESTAMP"))
        # backfill: contract was 'sent to bank' at its creation time (only where unset)
        conn.execute(text("UPDATE contracts SET sent_to_bank_at = created_at "
                          "WHERE sent_to_bank_at IS NULL AND created_at IS NOT NULL"))
        print("   done.")

        print("6) normalization_backup table...")
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS normalization_backup ("
            " id SERIAL PRIMARY KEY, table_name VARCHAR(60), row_pk INTEGER,"
            " field VARCHAR(60), original_value TEXT, new_value TEXT,"
            " created_at TIMESTAMP DEFAULT NOW())"))
        print("   done.")

        print("7) migration_flags table (one-time backfill marker)...")
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS migration_flags ("
            " flag VARCHAR(80) PRIMARY KEY, applied_at TIMESTAMP DEFAULT NOW())"))
        print("   done.")


# One-time marker so the seed + data backfill runs exactly ONCE per database. The schema
# ALTERs above stay idempotent (safe every deploy); only the expensive/one-way data
# backfill is gated behind this flag.
BACKFILL_FLAG = "normalization_backfill_v1"


def _backfill_done(conn) -> bool:
    return conn.execute(
        text("SELECT 1 FROM migration_flags WHERE flag = :f"), {"f": BACKFILL_FLAG}
    ).first() is not None


def _mark_backfill_done(conn):
    conn.execute(
        text("INSERT INTO migration_flags (flag) VALUES (:f) ON CONFLICT (flag) DO NOTHING"),
        {"f": BACKFILL_FLAG})


def _backup(conn, table, pk, field, original, new):
    if original is None or str(original) == str(new):
        return
    conn.execute(text(
        "INSERT INTO normalization_backup (table_name, row_pk, field, original_value, new_value) "
        "VALUES (:t, :pk, :f, :o, :n)"),
        {"t": table, "pk": pk, "f": field, "o": str(original), "n": (str(new) if new is not None else None)})


def _seed_masters(db):
    print("\n7) seeding company + bank masters...")
    for name in SEED_COMPANIES:
        nn = _norm(name)
        for Model in (Importer, Supplier):
            if not db.query(Model).filter(Model.name_norm == nn).first():
                db.add(Model(name=name, name_norm=nn, needs_review=False))
    for name in SEED_BANKS:
        nn = _norm(name)
        if not db.query(Bank).filter(Bank.name_norm == nn).first():
            db.add(Bank(name=name, name_norm=nn, needs_review=False))
    db.commit()
    # also fold existing (noisy) lc_master.bank_name into the banks master via canon
    seen = {b.name_norm for b in db.query(Bank).all()}
    rows = db.execute(text("SELECT DISTINCT bank_name FROM lc_master WHERE bank_name IS NOT NULL AND bank_name <> ''")).fetchall()
    added = 0
    for (bn,) in rows:
        canon = norm_bank(bn)
        nn = _norm(canon)
        if nn and nn not in seen and canon != "(Unknown Bank)":
            db.add(Bank(name=canon, name_norm=nn, needs_review=False))
            seen.add(nn)
            added += 1
    db.commit()
    print(f"   companies seeded; banks total {db.query(Bank).count()} (+{added} from data).")


def _backfill_companies(db):
    print("\n8) backfilling company names -> canonical (with backup)...")
    with engine.begin() as conn:
        # lc_master importer + supplier
        n_imp = n_sup = 0
        for lc in db.query(LCMaster).all():
            if lc.importer_name:
                canon, _row, _rev = match_company(lc.importer_name, db, Importer)
                if canon and canon != lc.importer_name:
                    _backup(conn, "lc_master", lc.lc_id, "importer_name", lc.importer_name, canon)
                    lc.importer_name = canon
                    n_imp += 1
            if lc.supplier_name:
                canon, _row, _rev = match_company(lc.supplier_name, db, Supplier)
                if canon and canon != lc.supplier_name:
                    _backup(conn, "lc_master", lc.lc_id, "supplier_name", lc.supplier_name, canon)
                    lc.supplier_name = canon
                    n_sup += 1
        db.flush()
        # contracts buyer + supplier
        n_cb = n_cs = 0
        for c in db.query(Contract).all():
            if c.buyer_name:
                canon, _row, _rev = match_company(c.buyer_name, db, Importer)
                if canon and canon != c.buyer_name:
                    _backup(conn, "contracts", c.contract_id, "buyer_name", c.buyer_name, canon)
                    c.buyer_name = canon
                    n_cb += 1
            if c.supplier_name:
                canon, _row, _rev = match_company(c.supplier_name, db, Supplier)
                if canon and canon != c.supplier_name:
                    _backup(conn, "contracts", c.contract_id, "supplier_name", c.supplier_name, canon)
                    c.supplier_name = canon
                    n_cs += 1
        db.flush()
        # goods_declarations importer — use a COLUMN-scoped raw query (not the full ORM
        # entity) + raw UPDATE, so GD columns added by LATER migrations (which don't exist
        # yet at this point in the deploy chain) can't make this SELECT fail with
        # UndefinedColumn. See the migration ORM-ordering gotcha.
        n_gd = 0
        gd_rows = conn.execute(text(
            "SELECT gd_id, importer_name FROM goods_declarations "
            "WHERE importer_name IS NOT NULL")).fetchall()
        for gd_id, importer_name in gd_rows:
            canon, _row, _rev = match_company(importer_name, db, Importer)
            if canon and canon != importer_name:
                _backup(conn, "goods_declarations", gd_id, "importer_name", importer_name, canon)
                conn.execute(text("UPDATE goods_declarations SET importer_name=:c WHERE gd_id=:i"),
                             {"c": canon, "i": gd_id})
                n_gd += 1
        db.commit()
    print(f"   lc_master importer:{n_imp} supplier:{n_sup} | contracts buyer:{n_cb} supplier:{n_cs} | gd:{n_gd}")


def _backfill_items(db):
    print("\n9) backfilling item codes...")
    n_lp = flagged_lp = 0
    for p in db.query(LCProduct).all():
        source = p.product_name or p.product_code
        code, review = normalize_item(source)
        # If product_code is already a valid short code, honour it.
        pc = (p.product_code or "").strip().upper()
        if pc in VALID_ITEM_CODES:
            code, review = pc, False
        p.item_code = code
        p.item_review = bool(review)
        if code:
            n_lp += 1
        else:
            flagged_lp += 1
    n_ci = flagged_ci = 0
    for it in db.query(ContractItem).all():
        code, review = normalize_item(it.product_name)
        it.item_code = code
        it.item_review = bool(review)
        if code:
            n_ci += 1
        else:
            flagged_ci += 1
    db.commit()
    print(f"   lc_products coded:{n_lp} flagged:{flagged_lp} | contract_items coded:{n_ci} flagged:{flagged_ci}")


def run_migration():
    print("=" * 60)
    print("Normalization batch migration")
    print("=" * 60)
    _add_columns()

    # The data backfill (seed masters + overwrite existing company/item values) is a
    # ONE-TIME operation. On every subsequent deploy the schema ALTERs above still run
    # (idempotent), but we skip the expensive re-scan/re-fuzzy-match once it has run.
    with engine.begin() as conn:
        already = _backfill_done(conn)
    if already:
        print("\nData backfill already applied on this database — skipping "
              "(schema ensured). Ongoing normalization happens at import time.")
        print("\nMigration complete.")
        return

    with Session(engine) as db:
        _seed_masters(db)
        _backfill_companies(db)
        _backfill_items(db)
    with engine.begin() as conn:
        _mark_backfill_done(conn)
    print("\nMigration complete (data backfill applied once; future deploys skip it).")


if __name__ == "__main__":
    run_migration()
