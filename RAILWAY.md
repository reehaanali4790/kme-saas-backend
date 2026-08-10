# Railway deployment — KME SaaS Backend

Deploy this repo as a **Docker** service on Railway. The repo includes `railway.json`, `Dockerfile`, and migration scripts wired for Railway's pre-deploy step.

## 1. Create the project

1. [Railway](https://railway.com) → **New Project** → **Deploy from GitHub** → select `kme-saas-backend`.
2. Add **PostgreSQL** to the project (Railway plugin).
3. (Recommended) Add a **Volume** mounted at `/data` for uploaded documents (BL, invoices, branding). The app auto-uses `/data` when that path exists.

## 2. Link Postgres to the web service

On the **backend web service**, add variables (Railway can reference the Postgres service):

| Variable | Value |
|----------|--------|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `DATABASE_PUBLIC_URL` | `${{Postgres.DATABASE_PUBLIC_URL}}` |

`DATABASE_PUBLIC_URL` is required for **pre-deploy migrations** when using the public proxy. If you omit it, pre-deploy skips migrations and they run automatically at container start (internal URL works at runtime).

## 3. Required environment variables

Copy from `railway.env.example` or set in Railway → **Variables**:

```env
ENVIRONMENT=production
DEBUG=false
SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_urlsafe(64))">
ALLOWED_ORIGINS=https://your-frontend.up.railway.app
APP_PUBLIC_URL=https://your-frontend.up.railway.app
ENABLE_SCHEDULER=true
```

| Variable | Purpose |
|----------|---------|
| `ENVIRONMENT` | Must be `production` on Railway |
| `DEBUG` | Must be `false` in production |
| `SECRET_KEY` | JWT signing — 32+ random characters |
| `ALLOWED_ORIGINS` | Frontend origin(s), comma-separated — **not** `*` |
| `APP_PUBLIC_URL` | Public frontend URL (Stripe redirects, signup emails) |
| `ENABLE_SCHEDULER` | `true` on **one** web instance; `false` if you scale replicas |

## 4. Optional but recommended

| Variable | Purpose |
|----------|---------|
| `REDIS_URL` | Upstash Redis (caching, rate limits) |
| `ANTHROPIC_API_KEY` | Document AI fallback |
| `GEMINI_API_KEY` | Primary document extraction |
| `STRIPE_SECRET_KEY` | Billing |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook verification |
| `STRIPE_PRICE_OPS_MONTHLY` / `ANNUAL` | Operations plan Stripe Price IDs |
| `STRIPE_PRICE_TD_MONTHLY` / `ANNUAL` | Trade Desk plan Stripe Price IDs |
| `PLATFORM_ADMIN_EMAILS` | Comma-separated super-admin emails |
| `RAILWAY_VOLUME_MOUNT_PATH` | Set to `/data` if volume mounted elsewhere |

## 5. Stripe webhook (billing)

After deploy, in Stripe Dashboard → **Developers → Webhooks**:

- **URL:** `https://<your-backend-domain>/api/billing/webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
- Copy the signing secret → `STRIPE_WEBHOOK_SECRET`

## 6. How deploy works

| Step | Script | What happens |
|------|--------|----------------|
| Build | `Dockerfile` | Python 3.11, Playwright Chromium, installs deps |
| Pre-deploy | `predeploy_railway.sh` | Swaps to `DATABASE_PUBLIC_URL`, runs `deploy_migrate.py` |
| Start | `start_railway.sh` | Validates settings, starts uvicorn on `$PORT` |
| Health | `/health` | DB ping via `platform` schema |

Migrations are **additive and idempotent** — safe on every deploy. Fresh databases get `platform`, `shared`, and default tenant `tenant_default` schemas automatically.

## 7. First login

After first deploy, create an admin user via Railway **Shell** on the web service:

```bash
cd backend
python -c "
from config.database import SessionLocal
from modules.auth.services import AuthService
from models.platform_models import Organization, OrganizationMembership, User
db = SessionLocal()
auth = AuthService(db)
org = db.query(Organization).filter(Organization.slug == 'default').first()
user = auth.create_user(email='admin@example.com', password='ChangeMeNow!', full_name='Admin')
db.add(OrganizationMembership(organization_id=org.organization_id, user_id=user.user_id, role='admin'))
db.commit()
print('Created admin@example.com')
"
```

Change the password immediately after first login.

## 8. Connect the frontend

Give the frontend service:

- `API_URL=https://<your-backend-public-domain>` (no trailing slash)

And ensure this backend has:

- `ALLOWED_ORIGINS` including the frontend public URL
- `APP_PUBLIC_URL` set to that same frontend URL

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Pre-deploy fails on DB | Add `DATABASE_PUBLIC_URL`; confirm Postgres is linked |
| App won't start: production safety | Set `DEBUG=false`, real `SECRET_KEY`, explicit `ALLOWED_ORIGINS` |
| Health check degraded | Postgres not linked or migrations failed — check deploy logs |
| Uploads lost on redeploy | Attach a volume at `/data` |
| Duplicate cron jobs | Only one instance should have `ENABLE_SCHEDULER=true` |
