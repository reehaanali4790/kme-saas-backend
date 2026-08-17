# UAT Checklist — First Client Pilot

Sign off each item with **Pass / Fail / N/A** and tester name + date.

## Authentication and access

| # | Test | Pass |
|---|------|------|
| 1 | Admin can log in | |
| 2 | Operator can log in | |
| 3 | Viewer can log in (read-only) | |
| 4 | Viewer cannot create shipment (403) | |
| 5 | Logout clears session | |

## LC and contracts

| # | Test | Pass |
|---|------|------|
| 6 | Create LC manually | |
| 7 | Import LCs from Excel | |
| 8 | LC table search/filter works | |
| 9 | Create / link contract | |
| 10 | LC detail shows linked contract | |

## Shipments

| # | Test | Pass |
|---|------|------|
| 11 | Create LC-backed shipment | |
| 12 | Shipment list filters by status | |
| 13 | Shipment detail journey timeline loads | |
| 14 | Create non-LC shipment (if in scope) | |
| 15 | Soft-delete and restore shipment | |

## Documents

| # | Test | Pass |
|---|------|------|
| 16 | Upload BL with AI extraction | |
| 17 | Upload commercial invoice | |
| 18 | Upload packing list | |
| 19 | Manual stub entry (file optional) | |
| 20 | Re-upload shows conflict resolution | |
| 21 | Open document in new tab | |

## GD / customs (if in scope)

| # | Test | Pass |
|---|------|------|
| 22 | Upload GD View | |
| 23 | Upload Item Details | |
| 24 | Advance GD workflow step | |
| 25 | Into-bond / ex-bond flow (if used) | |

## Workflow and exceptions

| # | Test | Pass |
|---|------|------|
| 26 | Blocked step shows workflow message | |
| 27 | My Work shows deadlines | |
| 28 | Exception banner on shipment (if applicable) | |
| 29 | Exceptions queue page lists items | |

## Reports and alerts

| # | Test | Pass |
|---|------|------|
| 30 | Dashboard loads without error | |
| 31 | At least one operational report runs | |
| 32 | LC/shipment alert appears when expected | |

## Admin

| # | Test | Pass |
|---|------|------|
| 33 | Admin can invite / manage users | |
| 34 | Branding logo displays on login | |
| 35 | WhatsApp config (if enabled) | |

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Client admin | | | |
| Vendor lead | | | |

**Pilot acceptance criteria:** Items 1–21 Pass; no critical (data loss / wrong-tenant) defects open.
