# Database migrations

This backend uses **Alembic** for new schema changes. Legacy hand-written scripts in
`scripts/migrations/` still run during deploy for existing single-schema upgrades.

## Layout

| Path | Purpose |
|------|---------|
| `alembic.ini` | Alembic config (run from `backend/`) |
| `alembic/env.py` | DB URL, ORM metadata, `version_table_schema=platform` |
| `alembic/versions/` | Revision chain |
| `infrastructure/migrations/helpers.py` | Multi-tenant DDL helpers |
| `deploy_migrate.py` | Deploy runner: legacy scripts → stamp → `alembic upgrade head` |

## Multi-tenant model

- **Platform / shared** tables use explicit schemas in ORM models.
- **Tenant** tables have no schema in metadata; migrations loop `platform.organizations`
  and `SET search_path TO tenant_*` before DDL (see `002_exception_paths.py`).
- `platform.alembic_version` stores a single global revision for the whole database.

## Deploy (Railway)

`predeploy_railway.sh` and `start_railway.sh` call `python deploy_migrate.py`, which:

1. Runs legacy bootstrap / upgrade scripts (skipped on fresh SaaS when `tenant_default` exists).
2. Stamps existing DBs that predate Alembic (detects `import_mode` column → stamp head).
3. Runs `alembic upgrade head`.

## Local commands

From `backend_software/backend`:

```bash
# Apply all pending revisions
PYTHONPATH=. alembic upgrade head

# Current revision
PYTHONPATH=. alembic current

# New revision (platform/shared autogenerate; add tenant DDL manually)
PYTHONPATH=. alembic revision --autogenerate -m "describe change"

# Stamp an existing DB without running migrations (use with care)
PYTHONPATH=. alembic stamp head
```

## Adding a tenant-scoped change

1. `alembic revision -m "your change"`
2. In `upgrade()`, use helpers from `infrastructure/migrations/helpers.py`:
   - `tenant_schemas_with_table(conn, "shipments")`
   - `set_tenant_search_path(conn, schema)`
   - Prefer `ADD COLUMN IF NOT EXISTS` for idempotency.
3. Deploy — `deploy_migrate.py` runs `upgrade head` on every release.

## New tenant provisioning

`provision_tenant()` creates tables via SQLAlchemy `create_all` at the current ORM
definition. No per-tenant Alembic replay is required when the global revision is already
at head; future revisions loop all tenant schemas on deploy.
