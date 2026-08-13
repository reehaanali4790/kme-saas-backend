"""
Deploy migration runner — runs all schema migrations in dependency order.

Use as the PaaS *release* command (Heroku/Railway/Render). Every migration here is
ADDITIVE and idempotent (CREATE TABLE for new tables, ADD COLUMN IF NOT EXISTS for new
columns), so this is safe to run on the live database on every deploy WITHOUT touching
existing rows. If any step fails, this exits non-zero so the platform aborts the release.

Run locally:  python backend/deploy_migrate.py
Release cmd:  cd backend && python deploy_migrate.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Fresh SaaS deploys bootstrap the full ORM schema in platform/shared/tenant_* via these
# two scripts. Legacy migrations below target the old single-schema public layout and are
# only needed when upgrading an existing single-tenant database.
SAAS_BOOTSTRAP_MIGRATIONS = frozenset({
    "migrate_platform_schema.py",
    "migrate_single_tenant_to_schema.py",
    "migrate_ai_usage_events.py",
})

# Order matters: bill_of_ladings must exist before shipment_tables links it; shipments
# must exist before demurrage/import-improvements add to/reference it.
MIGRATIONS = [
    "migrate_platform_schema.py",
    "migrate_single_tenant_to_schema.py",
    "migrate_bl_table.py",
    "migrate_shipment_tables.py",
    "migrate_system_alerts.py",
    "migrate_demurrage.py",
    "migrate_import_improvements.py",
    "migrate_packing_line_fields.py",
    "migrate_aed_rate.py",
    "migrate_lme_bulletin_metrics.py",# current bulletin extraction counters + currency rate
    "migrate_lc_amount.py",          # safe/additive: adds lc_products.lc_amount + backfills
    "migrate_lc_product_tracking.py",# lc_products shipment quantities + LME baseline/change fields
    "migrate_drop_lc_number_unique.py",  # allow duplicate LC numbers (matches current ORM)
    "migrate_gd_enhancements.py",    # GD exam/lab/gd_type cols + gd_attachments/edb_approvals/sro_quota_items
    "migrate_contract_lc.py",        # contracts + contract_items tables + lc_master.source/contract_id
    "migrate_lookups.py",            # importers/suppliers/indentors/booked_by name masters + seed
    "migrate_lc_reimbursement.py",   # lc_master.reimbursement + ensure lc_master.insurance
    "migrate_gd_status_and_vessel_loc.py",  # GD: drop PAID, OUT_OF_CHARGE->RELEASED; shipments.vessel_location
    "migrate_widen_hs_code.py",      # hs_code VARCHAR(20)->VARCHAR(50) (multi-code invoices overflowed 20)
    "migrate_insurance.py",          # insurance_certificates table (marine insurance per shipment)
    "migrate_gd_items.py",           # gd_items table + goods_declarations.levy_breakdown (per-item GD detail)
    "migrate_delivery_date.py",      # shipments.delivery_date + DELIVERED status
    "migrate_gd_attachment_kinds.py",# widen gd_attachments kind constraint (GD_VIEW/ITEM_DETAILS/ASSESSMENT)
    "migrate_shipment_docs_activity.py",  # shipment.etd, insurance.gross_premium, shipment_documents + activity_logs
    # Must precede normalization: that migration loads the full LCMaster ORM model,
    # which includes these buyer-allocation columns.
    "migrate_buyer_allocation.py",     # lc_master buyer_allocation_type/basis + lc_buyer_allocations
    "migrate_normalization_batch.py",  # company/item normalization, shipment manual fields+CLOSED, contract bank timeline, banks master
    "migrate_bank_limits.py",          # bank_limits + bank_limit_lines (Bank Limit Management)
    "migrate_dedupe_masters.py",       # collapse duplicate importer/supplier master rows (clean lookup dropdowns)
    "migrate_shipment_status_gd_weboc.py",  # shipments remarks/is_deleted + new auto statuses; GD FINAL_GD attachment kind
    "migrate_bank_limit_types.py",     # bank_limits bank_limit_type/lc_type + bank_limit_lines lc_type + PARENT/SUB/CHILD
    "migrate_weboc_sro.py",            # GD View/Item Details cols, ex_bond_entries, SRO company/main+group SRO numbers
    "migrate_widen_lc_fields.py",      # lc_master/lc_products: LC clause fields -> TEXT, names/ids widened (varchar(50) overflow on LC create)
    "migrate_kpt_tracking.py",         # shipments KPT milestone timestamps for document alerts
    "migrate_ib_ex_bond_gd.py",        # into_bond_gd_uploaded, ex_bond attachment_id, new kinds
    "migrate_bond_penalty.py",         # GD bond_penalty_* — 180-day breach penalty (amount/source/reason)
    "migrate_kgtl_weighments.py",      # gd_kgtl_weighments — own vs KGTL weighbridge per vehicle
    "migrate_eb_duty.py",              # ex_bond_entries duties_paid_pkr/duty_source — GD Balance report
    "migrate_perf_indexes.py",         # perf audit: missing indexes on expiry/eta/filing-date columns (CONCURRENTLY)
    "migrate_trgm_search_indexes.py",  # perf audit: pg_trgm + GIN indexes so ilike('%term%') searches use an index scan
    "migrate_vessel_status_source.py", # shipments.vessel_status_source/vessel_status_updated_at (Website/Manual indicator)
    "migrate_lme_bulletin_source.py",  # lme_bulletins.source (MANUAL/WEB) + UNIQUE(bulletin_date, source)
    "migrate_gd_verification.py",      # goods_declarations is_verified/verified_at/verified_by
    "migrate_document_upload_dates.py",  # bill_of_ladings/commercial_invoices/packing_lists upload_date (manual override)
    "migrate_shipment_eta_autocalc.py",  # shipments.transit_days + eta_source (auto-ETA estimation)
    "migrate_partial_gd.py",           # Partial GD (Ex-Bond Release): ex_bond_items table, ex_bond_entries
                                        # approval_id/matched_sro_no/is_finalized, gd_attachments.ex_bond_entry_id
    "migrate_partial_gd_unique_gd_number.py",  # unique EB GD Number per Into-Bond GD (case/whitespace-insensitive)
    "migrate_bl_type.py",              # bill_of_ladings.bl_type (COIL/CONTAINER) + backfill from is_container_bl()
    "migrate_ai_usage_events.py",      # platform.ai_usage_events for SaaS Admin Suite AI metering
]


def _fresh_saas_tenant_schema() -> bool:
    """True when tenant tables were created by ORM bootstrap (not legacy public upgrade)."""
    try:
        from sqlalchemy import text
        from config.database import engine
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'tenant_default' AND table_name = 'lc_master'"
            )).first()
            return row is not None
    except Exception as exc:
        print(f"  WARN: could not detect tenant schema ({exc})")
        return False


def main():
    print("=" * 60)
    print("Running deploy migrations (additive / idempotent)")
    print("=" * 60)
    env = os.environ.copy()
    # Always point subprocesses at the backend package root.
    env["PYTHONPATH"] = HERE
    # Migrations load config.settings — never let production safety checks block deploy.
    env.setdefault("ENVIRONMENT", "development")
    env.setdefault("DEBUG", "false")
    env.setdefault("ALLOWED_ORIGINS", "http://localhost")
    skip_legacy_public = False
    for script in MIGRATIONS:
        path = os.path.join(HERE, "scripts", "migrations", script)
        if not os.path.exists(path):
            print(f"  SKIP (not found): {script}")
            continue
        if skip_legacy_public and script not in SAAS_BOOTSTRAP_MIGRATIONS:
            print(f"\n--- {script} ---")
            print("  SKIP — multi-tenant SaaS schema already bootstrapped (ORM models).")
            continue
        print(f"\n--- {script} ---")
        result = subprocess.run([sys.executable, path], cwd=HERE, env=env)
        if result.returncode != 0:
            print(f"\nMIGRATION FAILED: {script} (exit {result.returncode}). Aborting release.")
            sys.exit(result.returncode)
        if script in SAAS_BOOTSTRAP_MIGRATIONS and _fresh_saas_tenant_schema():
            skip_legacy_public = True
            print("  Fresh SaaS tenant schema detected — skipping legacy public-schema migrations.")
    print("\n" + "=" * 60)
    print("All migrations applied successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
