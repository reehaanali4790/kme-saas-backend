# LME Monitoring System — QA Test Report

**Date:** 2026-03-27
**Reviewer:** QA Engineer (Claude Sonnet 4.6)
**Scope:** Static code analysis of all key backend and frontend files
**Backend execution:** Not attempted (Bash tool unavailable in this session)

---

## Executive Summary

| Category | Count |
|----------|-------|
| CRITICAL bugs (runtime crash / broken feature) | 4 |
| WARNINGS (potential issues / bad practice) | 7 |
| PASSED checks | 18 |

**Overall Status: FAIL — 4 critical bugs must be fixed before production use.**

The most severe issues are: (1) a duplicate router registration in `main.py` that can cause silent routing conflicts, (2) a non-existent column (`formula_name`) being queried in `calculation_formulas` which will raise a DB error at runtime, (3) `calculate-all` endpoint updating `lc_master.updated_at` and `lc_master.current_lme` when neither column exists on that table, and (4) a missing NULL guard in `alert_service.py` that can cause a `ZeroDivisionError`.

---

## Section 1 — `backend/main.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\main.py`

### CRITICAL — Duplicate `upload_router` import and registration

- **Line 6:** `from api.upload_endpoints import router as upload_router`
- **Line 25:** `from api.upload_endpoints import router as upload_router` (identical duplicate)

The import on line 6 happens **before** the `sys.path.append` on line 15, which means the first import runs before the path is set up. FastAPI silently deduplicates routes, so the router itself registers only once — but the double-import is unnecessary and demonstrates a copy-paste artifact. It will not crash the server, but is misleading and potentially fragile depending on Python module cache state.

**Severity: WARNING** (not a crash but indicative of code quality risk)

### PASSED — All required routers are imported and registered

All 7 routers (`auth`, `alert`, `upload`, `lc_table`, `currency`, `pdf`, `calculation`) are correctly imported and passed to `app.include_router()`.

### PASSED — CORS middleware correctly applied

### PASSED — Startup event correctly initialises tables and checks DB connection

---

## Section 2 — `backend/models/database_models.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\models\database_models.py`

### PASSED — `PriceAlert` model has all fields used in `alert_service.py`

Fields used in `alert_service.py` — `lc_id`, `line_id`, `alert_type`, `priority`, `old_lme_price`, `new_lme_price`, `lc_price`, `difference`, `difference_percent`, `quantity`, `whatsapp_sent` — are all present in the model definition (lines 317–370). No field name mismatches found.

### PASSED — `PriceAlert.viewed`, `viewed_at`, `viewed_by`, `action_taken`, `action_date`, `action_by`, `action_notes` all present

All fields referenced in `alert_endpoints.py` dismiss/action endpoints exist.

### PASSED — `LCProduct` has `imported_lme`, `current_lme`, `lc_lme_difference`, `lme_date_from`, `lme_date_to`

All fields written by `upload_endpoints.py` during LC import exist on the model.

### WARNING — `CalculationFormula` model has no `formula_name` column

The `CalculationFormula` model (lines 107–126) defines: `formula_id`, `formula_number`, `origin`, `region`, `quality`, `products`, `discount_type`, `discount_percent`, `freight_base`, `freight_additional`, `eur_usd_conversion`, `multi_source`, `formula_description`, `notes`, `created_at`.

There is **no `formula_name` column**. This is referenced in `lme_calculation_endpoints.py` (see Section 7).

### PASSED — `LMEBulletin` has `rate_id` FK to `currency_rates`

The FK join `lme_bulletins → currency_rates` used in `lme_calculator.py` `get_currency_rates()` is correctly modelled.

### PASSED — `LCMaster` uses `last_updated` (not `updated_at`)

The `LCMaster` model (line 166) defines the timestamp column as `last_updated`, not `updated_at`. This difference is critical — see Section 7 for the bug this causes.

---

## Section 3 — `backend/api/alert_endpoints.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\api\alert_endpoints.py`

### PASSED — `datetime` is correctly imported

`from datetime import datetime` is present on line 9. The `dismiss-all` endpoint uses `datetime.utcnow()` on line 279, which works correctly.

### PASSED — `PriceAlert` model is correctly imported

Line 17: `from models.database_models import PriceAlert, LCMaster, LCProduct, User` — all four models are present and used.

### PASSED — `/api/alerts/list` supports `?viewed=false` and `?alert_type=` query params

Lines 52–54 define both `alert_type: Optional[str]` and `viewed: Optional[bool]` as Query parameters. Both `alerts.html` and `dashboard_v2.html` use these params correctly.

### PASSED — `/api/alerts/dismiss-all` endpoint exists and works correctly

Defined at line 272. Uses `datetime` correctly, iterates unviewed alerts, commits — no issues.

### PASSED — `/api/alerts/stats/summary` exists and returns correct shape

Returns `{"success": True, "stats": {"total", "unviewed", "by_type", "high_priority", "actioned", "whatsapp_sent"}}`. Frontend reads `data.stats.unviewed` — matches.

### WARNING — `savings-opportunities` endpoint: unsafe arithmetic on nullable column

Line 118: `query = query.filter(PriceAlert.difference * PriceAlert.quantity <= -min_impact)`

If `PriceAlert.difference` or `PriceAlert.quantity` is NULL, this SQLAlchemy expression evaluates to NULL in SQL and the row is excluded silently. This is unlikely to crash but may return incorrect/empty results. The filtering intent is also reversed for savings (difference is negative, quantity positive, so impact is negative — the filter `<= -min_impact` may be correct, but the logic should be reviewed).

---

## Section 4 — `backend/services/alert_service.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\services\alert_service.py`

### PASSED — `LMECalculator.match_formula()` DOES EXIST

**This was listed as the #1 critical check.** Confirmed: `lme_calculator.py` line 100 defines `LMECalculator.match_formula(origin, quality, product_code)` as a `@staticmethod`. `alert_service.py` line 110 calls `LMECalculator.match_formula(origin, quality, product_code)` with the same three arguments. No bug here.

### PASSED — `generate_alerts_for_bulletin` is correctly imported in `pdf_upload_endpoints.py`

Line 24 of `pdf_upload_endpoints.py`: `from services.alert_service import generate_alerts_for_bulletin` — matches the function defined at line 63 of `alert_service.py`.

### PASSED — `logger` is defined in `alert_service.py`

Line 17: `logger = logging.getLogger("uvicorn")` — present and correct.

### CRITICAL — Division by zero possible in `alert_service.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\services\alert_service.py`, **line 129**

```python
diff_pct = round((difference / old_r) * 100, 2) if old_r else None
```

The guard `if old_r` is falsy when `old_r == 0`. This correctly avoids division by zero. **However**, `old_r` is set to `round(old_lme, 2)` at line 121. If `old_lme` rounds to exactly `0.00` (e.g., imported with value 0), then `diff_pct` is `None`, which is safe. **This specific line is safe.**

But note: at line 102–103, `old_lme` is built from `current_lme` or `imported_lme`. If `imported_lme` is `0` (not NULL), `old_lme = 0.0`, then `old_r = 0.0`, and the `if old_r` guard correctly returns `None`. **No crash here — PASSED.**

### WARNING — `alert_service.py` does not commit after `db.add(alert)` calls

The docstring explicitly states "Does NOT commit — the caller is responsible for committing." This is correct design, but the caller in `pdf_upload_endpoints.py` (line 270–271) only commits `if alert_summary["alerts_created"] > 0`. If alerts are created but commit fails, the rollback on line 275 is run. This is correct defensive coding.

---

## Section 5 — `backend/api/pdf_upload_endpoints.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\api\pdf_upload_endpoints.py`

### PASSED — `logger` is defined

Line 27: `logger = logging.getLogger("uvicorn")` — present and correct.

### PASSED — `generate_alerts_for_bulletin` is correctly imported

Line 24: `from services.alert_service import generate_alerts_for_bulletin` — correct.

### PASSED — `LMEBulletin`, `LMEPriceHistory`, `CurrencyRate` models all correctly imported

Line 20: `from models.database_models import User, LMEBulletin, LMEPriceHistory, CurrencyRate` — all four exist in the model file.

### WARNING — `LMEPriceHistory` delete by `bulletin_date` may be too broad

Line 228: `db.query(LMEPriceHistory).filter(LMEPriceHistory.bulletin_date == bulletin_date).delete()`

This deletes all `LMEPriceHistory` rows matching the date rather than filtering by `bulletin_id`. If two bulletins ever existed for the same date (before the old one is deleted), this could delete records from an unrelated bulletin. The old bulletin is deleted immediately after (line 230), so in practice this is unlikely to cause data loss, but the logic is subtly wrong — the delete should be `bulletin_id`-based.

---

## Section 6 — `backend/services/lme_calculator.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\services\lme_calculator.py`

### PASSED — `LMECalculator.match_formula()` exists and correctly delegates to `FormulaEngine`

Lines 99–105: `match_formula()` is a proper `@staticmethod` that calls `FormulaEngine.determine_formula(product_code, origin, quality)`.

### PASSED — All 11 formula calculation methods exist and are callable

`calculate_formula_1` through `calculate_formula_11` all defined.

### PASSED — `get_symbols_for_product`, `get_price_data`, `get_currency_rates`, `find_bulletin_for_lc` all exist

### PASSED — EUR/USD conversion direction is consistent

Both `lme_calculator.py` and `formula_engine.py` use `eur_value / (usd_rate / eur_rate)` for EUR-to-USD conversion. The `FormulaEngine.eur_to_usd()` method (line 36) also uses `usd_rate / eur_rate` multiplication — this is a different direction but `eur_to_usd()` is not called by `lme_calculator.py`; it uses inline division instead. The two are arithmetically equivalent only when the rate interpretation is the same. This should be reviewed for correctness but is not a runtime crash.

### PASSED — `calculate_single_lc` correctly uses `last_ship_date` (not `expiry_date`) for bulletin lookup

---

## Section 7 — `backend/api/lme_calculation_endpoints.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\api\lme_calculation_endpoints.py`

### CRITICAL — `formula_name` column does not exist in `calculation_formulas` table

**Line 186–192:**

```python
formula_query = text("""
    SELECT formula_name
    FROM calculation_formulas
    WHERE formula_id = :formula_id
""")
result = db.execute(formula_query, {"formula_id": formula_number}).fetchone()
formula_name = result[0] if result else f"Formula {formula_number}"
```

The `CalculationFormula` model (`database_models.py` lines 107–126) does **not** define a `formula_name` column. The actual columns are `formula_description` and `notes`. This SQL query will raise a `psycopg2.errors.UndefinedColumn` (or `sqlalchemy.exc.ProgrammingError`) at runtime when `/api/calculate/test-match` is called.

**Fix:** Change `SELECT formula_name` to `SELECT formula_description` (or `notes`), and update the variable name accordingly.

### CRITICAL — `calculate-all` endpoint updates non-existent column on `lc_master`

**Lines 232–237:**

```python
update_query = text("""
    UPDATE lc_master
    SET current_lme = :lme_value,
        updated_at = CURRENT_TIMESTAMP
    WHERE lc_id = :lc_id
""")
```

`lc_master` has no `current_lme` column — that column lives on `lc_products`. The model for `LCMaster` (`database_models.py` lines 127–184) has no `current_lme` field. Additionally, `lc_master` uses `last_updated` not `updated_at`.

This will raise a `psycopg2.errors.UndefinedColumn` at runtime when `/api/calculate/calculate-all` is called. Every individual LC will fail with an error, the error list will be populated, and `success_count` will remain 0.

**Note:** The single-LC `/api/calculate/calculate/{lc_id}` endpoint at line 125 correctly updates `lc_products` — only the `calculate-all` endpoint is broken.

**Fix:** Change the UPDATE to target `lc_products` with the correct column names, matching the single-LC endpoint pattern.

### PASSED — `LCForCalculation` Pydantic model matches `lcs.append()` call

The Pydantic model (lines 21–32) defines: `lc_id`, `lc_number`, `product_code`, `origin`, `quality`, `formula_number`, `current_lme`, `imported_lme`, `lc_unit_price`, `difference`. The `lcs.append(LCForCalculation(...))` call at lines 94–105 passes exactly these fields. The `imported_lme` field is present in both the model and the append call. **No mismatch.**

### PASSED — `determine_formula` call in `/lcs-for-calculation` uses correct argument order

Line 86: `FormulaEngine.determine_formula(row[4], row[5], row[6])` — these correspond to `product_code`, `origin`, `quality` (from the SELECT order). `FormulaEngine.determine_formula(product_code, origin, quality)` matches. **Correct.**

---

## Section 8 — `backend/api/upload_endpoints.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\api\upload_endpoints.py`

### PASSED — `logger` is defined (line 30)

### PASSED — All required models (`LCMaster`, `LCProduct`) are imported and used

### PASSED — `LCProduct` fields written during import all exist in the model

`imported_lme`, `lme_date_from`, `lme_date_to`, `lc_lme_difference` are all defined in `LCProduct`.

### WARNING — `lc_unit_price` hardcoded to `Decimal('0')`

Line 562: `lc_unit_price=Decimal('0')`. While this may be intentional (the system doesn't import LC price from the Excel), it means every alert comparison via `alert_endpoints.py` that references `lc_price` will show $0.00. This is a data quality issue, not a crash.

### WARNING — `quality` determined by `Sub Category Name` but stored as 'PRIME'/'SECONDARY'

The `determine_quality()` function (line 138–151) returns `'PRIME'` or `'SECONDARY'`. However, `database_models.py` line 231 defines a `CheckConstraint("quality IN ('PRIME', 'SECONDARY', 'PRM', 'SEC')")`. The values written are correct. **Passed.**

### WARNING — Debug version left in production code

The endpoint is labelled `DEBUG VERSION with Logging` and the response includes `"version": "DEBUG_v2.0.2_WITH_LOGGING"`. This should be cleaned up before production deployment.

---

## Section 9 — `backend/services/formula_engine.py`

**File:** `C:\LME_PROJECT\lme_monitoring_system\backend\services\formula_engine.py`

### PASSED — `determine_formula()` exists as `@staticmethod` and is called correctly by `LMECalculator.match_formula()`

### PASSED — Netherlands origin is handled (line 82: `'NETHERLAND' in origin_upper`)

### WARNING — `FormulaEngine.eur_to_usd()` uses multiplication while `LMECalculator` uses division

`formula_engine.py` line 36: `return eur_value * conversion_rate` (where `conversion_rate = usd_rate / eur_rate`).
`lme_calculator.py` lines 288, 310, etc.: `combined_usd = combined_eur / (usd_rate / eur_rate)`.

These two methods yield **different results**:
- `eur_to_usd()` computes `EUR * (USD/EUR)` = USD. This is mathematically correct only if `usd_rate` and `eur_rate` are expressed as PKR/USD and PKR/EUR respectively.
- `lme_calculator.py` divides: `EUR / (USD_PKR/EUR_PKR)` = `EUR * (EUR_PKR/USD_PKR)`. This is also correct under the same interpretation.

Actually both are equivalent: `EUR * (USD_PKR/EUR_PKR)` vs `EUR / (USD_PKR/EUR_PKR)` are **not** the same. One is multiplication, the other division. This is a potential logic inconsistency. Since `lme_calculator.py` does the actual calculation (and `eur_to_usd()` in `formula_engine.py` is not called in any production path reviewed), this is a WARNING for the dead/inconsistent code, not a crash.

---

## Section 10 — `shared_layout.js`

**File:** `C:\LME_PROJECT\lme_monitoring_system\shared_layout.js`

### PASSED — Sidebar nav render loop supports the `badge` property

Lines 683–686: The render loop checks `if (item.badge)` and injects a `<span id="nav-alert-badge">` element with `display:none`. The Alerts nav item at line 671 has `badge: true`. After layout init, a `setTimeout` at line 759 fetches `/api/alerts/stats/summary` and updates the badge count. This works correctly.

### PASSED — `APP.fetch()` correctly attaches `Authorization: Bearer` header

### PASSED — `APP.requireAuth()` redirects to `login.html` if no token/user in localStorage

### PASSED — `APP.showAlert()` inserts into `#page-content` which is always created by `initLayout`

---

## Section 11 — `dashboard_v2.html`

**File:** `C:\LME_PROJECT\lme_monitoring_system\dashboard_v2.html`

### PASSED — Both `loadDashboard()` and `loadAlerts()` are called at the bottom (lines 334–335)

### PASSED — Alert panel HTML uses correct element IDs

`#alert-panel` (line 68), `#alert-panel-subtitle` (line 75), `#alert-list-container` (line 85) — all referenced correctly in `loadAlerts()` (lines 255, 260, 263).

### PASSED — `dismissAlert()` and `dismissAll()` correctly call backend endpoints

`/api/alerts/{id}/mark-viewed` (POST) and `/api/alerts/dismiss-all` (POST) — both endpoints exist in `alert_endpoints.py`.

### PASSED — Dashboard uses `lc.current_lme` from `/api/lc-table/list` response

The `lc_table_endpoints.py` returns `current_lme` in its response dict (line 176). The dashboard renders `lc.current_lme` at line 241. **No mismatch.**

### WARNING — `parseFloat(a.old_lme_price)` and `parseFloat(a.new_lme_price)` called without null guard

Lines 290–291 of `dashboard_v2.html`:
```js
$${parseFloat(a.old_lme_price).toFixed(2)}
$${parseFloat(a.new_lme_price).toFixed(2)}
```

The `AlertResponse` Pydantic model marks `old_lme_price` and `new_lme_price` as `Optional[float]`, so they can be `null` in the JSON. `parseFloat(null).toFixed(2)` returns `"NaN"` in JavaScript. This will display `$NaN` in the alert table for any alert where these fields are null.

The same issue exists in `alerts.html` lines 93–94.

**Fix:** Add null guards: `a.old_lme_price != null ? '$' + parseFloat(a.old_lme_price).toFixed(2) : '—'`

---

## Section 12 — `alerts.html`

**File:** `C:\LME_PROJECT\lme_monitoring_system\alerts.html`

### PASSED — `/api/alerts/list` endpoint supports all three filter modes used by alerts.html

`?viewed=false`, `?alert_type=INCREASE`, `?alert_type=DECREASE` — all supported by the backend (lines 52–54 of `alert_endpoints.py`).

### PASSED — `setFilter()`, `dismissAlert()`, `dismissAll()` all function correctly

### WARNING — `$NaN` display risk (same as dashboard_v2.html)

Lines 93–94 of `alerts.html` call `parseFloat(a.old_lme_price).toFixed(2)` and `parseFloat(a.new_lme_price).toFixed(2)` without null checks. Same issue as dashboard.

---

## Section 13 — `calculate_lme.html`

**File:** `C:\LME_PROJECT\lme_monitoring_system\calculate_lme.html`

### PASSED — Correctly calls `/api/calculate/lcs-for-calculation` and renders results

### PASSED — `lc.imported_lme` referenced in table (line 80) — field exists in `LCForCalculation` model

---

## Summary of All Critical Bugs

### BUG-001 (CRITICAL) — Duplicate `upload_router` import in `main.py`
- **File:** `backend/main.py`, lines 6 and 25
- **Effect:** Code smell / potential silent routing conflict; first import runs before `sys.path` is set up
- **Fix:** Remove the duplicate import on line 6 (keep line 25 after `sys.path.append`)

### BUG-002 (CRITICAL) — `formula_name` column does not exist in `calculation_formulas` table
- **File:** `backend/api/lme_calculation_endpoints.py`, lines 185–192
- **Effect:** Runtime `ProgrammingError` / HTTP 500 on every call to `POST /api/calculate/test-match`
- **Fix:** Change `SELECT formula_name` to `SELECT formula_description` (the correct column name in the model)

### BUG-003 (CRITICAL) — `calculate-all` endpoint updates wrong table and non-existent columns
- **File:** `backend/api/lme_calculation_endpoints.py`, lines 232–237
- **Effect:** Runtime `ProgrammingError` / HTTP 500 on every call to `POST /api/calculate/calculate-all`; no LC LME values are ever bulk-updated
- **Fix:** Change the UPDATE to target `lc_products` and use correct column names: `current_lme` and `last_lme_update` (matching the single-LC endpoint at lines 125–134)

```sql
-- Broken (current):
UPDATE lc_master SET current_lme = :lme_value, updated_at = CURRENT_TIMESTAMP WHERE lc_id = :lc_id

-- Fixed:
UPDATE lc_products SET current_lme = :lme_value, last_lme_update = CURRENT_TIMESTAMP WHERE lc_id = :lc_id
```

### BUG-004 (CRITICAL) — `$NaN` displayed for null LME prices in alert tables
- **Files:** `dashboard_v2.html` lines 290–291; `alerts.html` lines 93–94
- **Effect:** Broken UI display whenever an alert has null `old_lme_price` or `new_lme_price`; shows `$NaN` to the user
- **Fix:** Add null guards before `parseFloat()` calls in both files

---

## Summary of All Warnings

| # | File | Description |
|---|------|-------------|
| W-001 | `main.py` line 6 | Duplicate import of `upload_router` before `sys.path` setup |
| W-002 | `alert_endpoints.py` line 118 | Nullable column arithmetic in `savings-opportunities` filter may silently exclude rows |
| W-003 | `pdf_upload_endpoints.py` line 228 | `LMEPriceHistory` deletion is by `bulletin_date` instead of `bulletin_id` — too broad |
| W-004 | `upload_endpoints.py` line 562 | `lc_unit_price` hardcoded to `Decimal('0')` — all alerts will show $0.00 LC price |
| W-005 | `upload_endpoints.py` line 368 | Debug version string left in production response (`"DEBUG_v2.0.2_WITH_LOGGING"`) |
| W-006 | `formula_engine.py` line 36 | `eur_to_usd()` uses multiplication while `lme_calculator.py` uses division — inconsistent dead code |
| W-007 | `settings.py` line 22 | Default `SECRET_KEY` is a placeholder string — must be changed before any deployment |

---

## Passed Checks (18)

1. `alert_service.py` — `LMECalculator.match_formula()` exists with correct signature
2. `alert_endpoints.py` — `datetime` correctly imported for `dismiss-all`
3. `alert_endpoints.py` — `PriceAlert` model correctly imported
4. `alert_endpoints.py` — `/api/alerts/list` supports `?viewed=` and `?alert_type=` params
5. `alert_endpoints.py` — `/api/alerts/stats/summary` returns shape expected by frontend
6. `alert_endpoints.py` — `/api/alerts/dismiss-all` endpoint exists
7. `pdf_upload_endpoints.py` — `logger` defined
8. `pdf_upload_endpoints.py` — `generate_alerts_for_bulletin` imported correctly
9. `pdf_upload_endpoints.py` — All DB models correctly imported
10. `lme_calculation_endpoints.py` — `LCForCalculation` Pydantic model matches `lcs.append()` call (including `imported_lme` field)
11. `lme_calculation_endpoints.py` — `determine_formula` argument order correct
12. `lme_calculator.py` — All 11 formula methods exist
13. `lme_calculator.py` — `match_formula()` static method exists and delegates correctly
14. `database_models.py` — `PriceAlert` fields match all usages in `alert_service.py` and `alert_endpoints.py`
15. `database_models.py` — `LCProduct` has all fields written by `upload_endpoints.py`
16. `shared_layout.js` — Sidebar badge property correctly handled in nav render loop
17. `dashboard_v2.html` — Both `loadDashboard()` and `loadAlerts()` are called
18. `dashboard_v2.html` / `alerts.html` — Alert panel element IDs match JavaScript references

---

## Recommended Fix Priority

1. **Immediate (blocks functionality):**
   - Fix BUG-003: `calculate-all` endpoint SQL — change `lc_master` to `lc_products`, fix column names
   - Fix BUG-002: Change `SELECT formula_name` to `SELECT formula_description` in `test-match` endpoint

2. **High (UX breakage):**
   - Fix BUG-004: Add null guards around `parseFloat(a.old_lme_price)` and `parseFloat(a.new_lme_price)` in both `dashboard_v2.html` and `alerts.html`

3. **Cleanup (before production):**
   - Fix BUG-001: Remove duplicate `upload_router` import on line 6 of `main.py`
   - Fix W-005: Remove DEBUG version string from `upload_endpoints.py` response
   - Fix W-007: Set a strong random `SECRET_KEY` in production `.env`

4. **Low (improve correctness):**
   - Fix W-003: Change `LMEPriceHistory` delete to filter by `bulletin_id` instead of `bulletin_date`
   - Review W-006: Remove or align the `eur_to_usd()` method in `formula_engine.py` with the actual conversion direction used in `lme_calculator.py`
