# Client Onboarding Runbook — White-Glove Pilot

Use this checklist when provisioning the **first client** manually (no self-serve Stripe).

## Phase 1 — Discovery (Day 1–2)

- [ ] Confirm import workflow scope: LC-backed vs non-LC paths, GD/customs depth, reports needed
- [ ] Count users and roles (Admin, Manager, Operator, Viewer)
- [ ] Identify legacy data: Excel LCs only vs full shipment/GD history
- [ ] Agree pilot duration (recommended: 90 days) and support channel

## Phase 2 — Provision tenant (Day 2)

From `backend/`:

```bash
PYTHONPATH=. python -m modules.tenants.provision --slug client-slug --name "Client Legal Name" --plan enterprise
```

Or use **Platform console** → Organizations → Create org + admin user.

- [ ] Set branding (logo, app name) in tenant Admin → Branding
- [ ] Create user accounts for each role
- [ ] Verify login for each role

## Phase 3 — Data import (Day 3–5)

**LC master data:**
- [ ] Import via **LC Upload** (Excel) or manual LC create
- [ ] Validate LC table counts vs client spreadsheet

**Contracts (if applicable):**
- [ ] Upload contracts or enter manually
- [ ] Link LCs to contracts

**Shipments (if migrating active cargo):**
- [ ] Create shipments from LCs
- [ ] Upload BL / invoice / packing where available
- [ ] Enter GD status for in-progress customs work

**Not automated today:** full pg_dump migration from legacy DB — use Excel + manual entry for pilot.

## Phase 4 — UAT (Day 5–10)

Run [UAT_CHECKLIST.md](UAT_CHECKLIST.md) jointly with client power users.

## Phase 5 — Go-live (Day 10+)

- [ ] Production env vars verified (see [../OPERATIONS.md](../OPERATIONS.md))
- [ ] Backup taken before go-live
- [ ] Client admin trained on: create shipment, doc upload, My Work, exceptions
- [ ] Daily check-in for first 5 business days

## Phase 6 — Pilot review (Day 30–90)

- [ ] 10+ real shipments processed end-to-end
- [ ] Client sign-off on UAT checklist
- [ ] Decide: continue, expand users, or plan data migration tooling
