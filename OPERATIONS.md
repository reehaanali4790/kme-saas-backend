# Operations Guide — First Client Go-Live

Unified runbook for deploying and operating the LME Import SaaS platform.

## Architecture

| Service | Repo | Deploy |
|---------|------|--------|
| Backend API | `kme-saas-backend` | Railway Docker, `predeploy_railway.sh` + `start_railway.sh` |
| Frontend | `lme-saas-frontend` | Railway Docker, proxies `/api/*` to backend |

See also:
- [RAILWAY.md](RAILWAY.md)
- [backend/MIGRATIONS.md](backend/MIGRATIONS.md)

## Production environment checklist

**Backend (required):**
- `DATABASE_URL`, `DATABASE_PUBLIC_URL` (for pre-deploy migrations)
- `SECRET_KEY` (32+ random chars)
- `ENVIRONMENT=production`, `DEBUG=false`
- `ALLOWED_ORIGINS=https://your-frontend-domain.com`
- `APP_PUBLIC_URL=https://your-frontend-domain.com`
- Railway volume mounted at `/data` (uploads persistence)
- `REDIS_URL` (recommended for sessions)
- `SENTRY_DSN` (optional)

**Frontend:**
- `API_URL=https://your-backend-domain.com` (server-side proxy target)
- `NEXT_PUBLIC_SENTRY_DSN` (optional)

## Database backup and restore

### Postgres (Railway)

1. **Enable Railway automated backups** on the Postgres plugin (daily snapshots).
2. **Manual backup** before major releases:

```bash
pg_dump "$DATABASE_URL" -Fc -f backup_$(date +%Y%m%d).dump
```

3. **Restore to a new database** (test or disaster recovery):

```bash
pg_restore -d "$TARGET_DATABASE_URL" --clean --if-exists backup_YYYYMMDD.dump
```

4. After restore, run migrations: `cd backend && python deploy_migrate.py`

### Upload volume (`/data`)

User documents live under `/data/uploads/{tenant_schema}/` on the backend volume.

1. **Backup:** copy the volume snapshot or rsync `/data/uploads` to secure storage weekly.
2. **Restore:** mount volume, restore files to `/data/uploads`, ensure paths match DB `document_path` values.
3. **Verify:** upload a test file per tenant after restore; open document from shipment detail.

### Recovery objectives (pilot)

| Metric | Target |
|--------|--------|
| RPO (data loss) | 24 hours (daily backup) |
| RTO (restore time) | 4 hours (manual runbook) |

## Deploy procedure

1. Push to `main` → Railway auto-deploys both services.
2. Pre-deploy runs `deploy_migrate.py` via public DB URL.
3. Container start runs migrations again + uvicorn.
4. Verify: `GET /health` returns `{"status":"ok"}`.
5. Smoke test: login → shipments list → open one shipment.

## Rollback

1. Railway → service → **Deployments** → redeploy previous successful deployment.
2. If a bad migration ran, restore Postgres from last backup (migrations are additive; rollback is usually redeploy-only).
3. Notify client if any data entry occurred during the bad window.

## Client onboarding (pilot)

See [docs/CLIENT_ONBOARDING.md](docs/CLIENT_ONBOARDING.md) and [docs/UAT_CHECKLIST.md](docs/UAT_CHECKLIST.md).

## Support escalation

1. Check Railway logs (backend + frontend).
2. Platform owner console → Infra + Audit.
3. Optional: Sentry (when `SENTRY_DSN` configured).

## Scheduler note

Background jobs (NBP, LME, KPT, alerts) run in-process.

- With Redis (`REDIS_URL` set), replicas elect a single leader via `lme:scheduler:leader` (TTL 120s). Extra web replicas can keep `ENABLE_SCHEDULER=true`.
- Without Redis, set `ENABLE_SCHEDULER=true` on **one** instance only and `false` on the others.
- `ENABLE_SCHEDULER=false` always skips jobs on that process.
