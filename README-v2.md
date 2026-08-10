# 📘 LME MONITORING SYSTEM - MASTER REFERENCE

**Version:** 2.1 (Updated from code audit)
**Last Updated:** March 26, 2026
**Database:** PostgreSQL 18
**Backend:** Python 3.14.3, FastAPI
**Frontend:** React-style HTML Dashboard

---

## 🗄️ DATABASE STRUCTURE

### **Table: lc_master**
Primary LC information

| Column | Type | Notes |
|--------|------|-------|
| lc_id | integer | PRIMARY KEY |
| lc_number | varchar(50) | LC number (unique) |
| contract_number | varchar(50) | Contract reference |
| supplier_name | varchar(200) | Supplier name |
| importer_name | varchar(200) | Importer name |
| lc_date | date | LC issue date (not null) |
| expiry_date | date | LC expiry date |
| last_ship_date | date | Last shipment date |
| lc_amount | numeric(15,2) | Total LC value |
| currency | varchar(10) | Currency code |
| hoa | varchar(100) | Head of Agreement |
| payment_terms | varchar(200) | Payment terms |
| monitoring_expiry | date | Monitoring period end date (not null) |
| status | varchar(20) | OPEN, CLOSED, SHIPPED, EXPIRED, CANCELLED |
| status_changed_at | timestamp | When status last changed |
| status_changed_by | integer | FK → users.user_id |
| status_notes | text | Notes on status change |
| reopen_requested | boolean | Reopen request flag |
| reopen_requested_by | integer | FK → users.user_id |
| reopen_requested_at | timestamp | When reopen was requested |
| reopen_notes | text | Notes on reopen |
| created_by | integer | FK → users.user_id |
| assigned_to | integer | FK → users.user_id |
| import_date | timestamp | Record creation time |
| last_updated | timestamp | Last update time |

**⚠️ NOTE:** Status values are `OPEN, CLOSED, SHIPPED, EXPIRED, CANCELLED` — NOT `ACTIVE/MONITORING/EXPIRING/REOPENED`.

### **Table: lc_products**
Product details for each LC (can have multiple products per LC)

| Column | Type | Notes |
|--------|------|-------|
| line_id | integer | PRIMARY KEY |
| lc_id | integer | FOREIGN KEY → lc_master.lc_id |
| product_code | varchar(20) | HRP, CRS, GPS, WRLC, etc. (not null) |
| product_name | varchar(100) | Product description |
| origin | varchar(50) | CHINA, NETHERLANDS, UAE, etc. (not null) |
| quality | varchar(20) | PRIME, SECONDARY, PRM, SEC (not null) |
| cargo_nature | varchar(20) | Cargo nature |
| quantity | numeric(12,2) | Quantity (not null) |
| unit | varchar(10) | MT (metric tons) |
| **lc_unit_price** | numeric(10,2) | LC contract price (not null) |
| **current_lme** | numeric(10,2) | Current calculated LME price |
| current_lme_price | numeric(10,2) | Alternative LME field |
| imported_lme | numeric(10,2) | Original imported LME |
| lme_date_from | date | LME date range start |
| lme_date_to | date | LME date range end |
| previous_lme | numeric(10,2) | Previous LME value |
| lme_change_amount | numeric(10,2) | Change in amount |
| lme_change_percent | numeric(5,2) | Change in percentage |
| lc_lme_difference | numeric(10,2) | Difference between LC and LME |
| last_lme_update | timestamp | Last LME calculation time |
| change_detected | boolean | Flag for price change detection |
| hs_code | varchar(20) | Harmonized System code |
| grade | varchar(50) | Product grade |
| size | varchar(50) | Product size |

**⚠️ CRITICAL:** `current_lme` and `lc_unit_price` are in `lc_products` table, NOT `lc_master`!

### **Table: lme_bulletins**
Metal Bulletin price publications

| Column | Type | Notes |
|--------|------|-------|
| bulletin_id | integer | PRIMARY KEY |
| bulletin_date | date | Bulletin publication date (not null) |
| upload_date | timestamp | Upload time |
| file_name | varchar(255) | Original PDF filename (not null) |
| file_path | text | File storage path |
| file_size | integer | File size in bytes |
| uploaded_by | integer | FK → users.user_id |
| extracted_data | JSONB | Raw extracted JSON data |
| rate_id | integer | FK → currency_rates.rate_id |
| symbols_extracted | integer | Count of symbols extracted |
| prices_stored | integer | Count of prices stored |

**⚠️ NOTE:** `usd_rate` and `eur_rate` are NOT in `lme_bulletins`. They are in the `currency_rates` table, linked via `rate_id`.

### **Table: currency_rates**
USD/EUR exchange rates (separate from bulletins)

| Column | Type | Notes |
|--------|------|-------|
| rate_id | integer | PRIMARY KEY |
| rate_date | date | Rate date (unique, not null) |
| usd_rate | numeric(10,4) | USD to PKR rate (not null) |
| eur_rate | numeric(10,4) | EUR to PKR rate (not null) |
| source | varchar(50) | Source (default: Manual) |
| notes | text | Notes |
| created_at | timestamp | Creation time |
| created_by | integer | FK → users.user_id |
| updated_at | timestamp | Last update time |
| updated_by | integer | FK → users.user_id |

### **Table: lme_prices**
Individual price entries from bulletins

| Column | Type | Notes |
|--------|------|-------|
| price_id | integer | PRIMARY KEY |
| bulletin_id | integer | FOREIGN KEY → lme_bulletins.bulletin_id |
| fastmarket_symbol | varchar(50) | MB-STE-0144, MB-STE-0145, etc. |
| product_code | varchar(20) | Product code (not null) |
| origin | varchar(50) | Origin region (not null) |
| quality | varchar(20) | PRIME or SECONDARY |
| low_price | numeric(10,2) | Low price |
| high_price | numeric(10,2) | High price |
| calculated_lme | numeric(10,2) | Calculated LME result |
| formula_used | varchar(20) | Formula applied |
| calculation_date | timestamp | When calculated |
| eur_rate | numeric(10,6) | EUR/PKR rate used |
| usd_rate | numeric(10,6) | USD/PKR rate used |
| conversion_rate | numeric(10,6) | Computed conversion rate |
| notes | text | Additional notes |

**⚠️ NOTE:** Column names are `fastmarket_symbol`, `low_price`, `high_price` — NOT `symbol`, `price_low`, `price_high`!

### **Table: calculation_formulas**
Formula definitions

| Column | Type | Notes |
|--------|------|-------|
| formula_id | integer | PRIMARY KEY |
| formula_number | integer | Formula number (1-11, not null) |
| origin | varchar(50) | Origin region (not null) |
| region | varchar(10) | Region code (not null) |
| quality | varchar(20) | PRIME or SECONDARY (not null) |
| products | ARRAY(text) | Product codes list |
| discount_type | varchar(10) | Discount type |
| discount_percent | numeric(5,2) | Discount percentage |
| freight_base | numeric(10,2) | Base freight cost |
| freight_additional | numeric(10,2) | Additional freight |
| eur_usd_conversion | boolean | Whether EUR→USD conversion needed |
| multi_source | boolean | Whether uses multiple price sources |
| formula_description | text | Formula description |
| notes | text | Additional notes |
| created_at | timestamp | Record creation time |

**⚠️ NOTE:** Column is `formula_description` (not `formula_name`) and `quality` (not `quality_type`).

### **Table: price_alerts**
Price change alerts

| Column | Type | Notes |
|--------|------|-------|
| alert_id | integer | PRIMARY KEY |
| lc_id | integer | FK → lc_master.lc_id |
| line_id | integer | FK → lc_products.line_id |
| alert_date | timestamp | Alert creation time |
| alert_type | varchar(20) | DECREASE or INCREASE |
| priority | varchar(20) | HIGH, MEDIUM, LOW |
| old_lme_price | numeric(10,2) | Previous LME price |
| new_lme_price | numeric(10,2) | New LME price |
| lc_price | numeric(10,2) | LC contract price |
| difference | numeric(10,2) | Price difference |
| difference_percent | numeric(5,2) | Percentage difference |
| quantity | numeric(12,2) | Quantity for impact calculation |
| whatsapp_sent | boolean | WhatsApp delivery flag |
| viewed | boolean | Dashboard viewed flag |
| action_taken | varchar(50) | Action recorded |
| action_by | integer | FK → users.user_id |

### **Table: roles**
User roles with permissions

| Column | Type | Notes |
|--------|------|-------|
| role_id | integer | PRIMARY KEY |
| role_name | varchar(50) | Role name (unique) |
| can_import_lc | boolean | Permission flag |
| can_upload_pdf | boolean | Permission flag |
| can_view_dashboard | boolean | Permission flag |
| can_edit_lc | boolean | Permission flag |
| can_delete_lc | boolean | Permission flag |
| can_manage_users | boolean | Permission flag |
| can_configure_alerts | boolean | Permission flag |
| can_view_all_lcs | boolean | Permission flag |
| can_export_reports | boolean | Permission flag |
| can_reopen_lc | boolean | Permission flag |
| can_change_lc_status | boolean | Permission flag |

### **Other Tables**
| Table | Purpose |
|-------|---------|
| `users` | User accounts with role FK, contact info, preferences |
| `user_sessions` | JWT session tracking (login/logout times) |
| `whatsapp_config` | Per-user WhatsApp alert preferences |
| `audit_log` | Action audit trail (JSONB old/new values) |
| `lme_price_history` | Historical LME prices from bulletins |

---

## 🧮 FORMULA LOGIC

### **Formula Matching: Single Source of Truth**

**Location:** `backend/services/formula_engine.py`
**Method:** `FormulaEngine.determine_formula(product_code, origin, quality)`

**Returns:** Formula number (1-11)

### **Formula Matching Rules:**

#### **China Origin:**
```python
if 'CHINA' in origin_upper:
    if quality == 'PRIME':
        if product_code in ['HRP', 'CRP', 'PPGI', 'PPGIP', 'GPP', 'GLP', 'GP']:
            return 1  # Formula 1
        elif product_code == 'WRLC':
            return 4  # Formula 4
        elif product_code == 'WRHC':
            return 5  # Formula 5
    else:  # SECONDARY
        if product_code == 'CRNGO':
            return 3  # Formula 3
        else:  # HRS, CRS, GPS, GLS, PPGIS
            return 2  # Formula 2
```

#### **Europe Origin (INCLUDING NETHERLANDS!):**
```python
elif ('EUROPE' in origin_upper or
      'GERMAN' in origin_upper or
      'ITALY' in origin_upper or
      'SPAIN' in origin_upper or
      'NETHERLAND' in origin_upper):  # ← CRITICAL: Include Netherlands!
    if product_code in ['CRS', 'GPS']:
        return 6  # Formula 6
    elif product_code == 'HRS':
        return 7  # Formula 7
```

#### **Taiwan / South Africa Origin:**
```python
elif 'TAIWAN' in origin_upper or 'AFRICA' in origin_upper:
    if product_code in ['CRS', 'GPS']:
        return 8  # Formula 8
    elif product_code == 'HRS':
        return 9  # Formula 9
```

#### **UAE / Iran Origin:**
```python
elif 'UAE' in origin_upper or 'IRAN' in origin_upper:
    if product_code == 'WRLC':
        return 10  # Formula 10
    elif product_code == 'WRHC':
        return 11  # Formula 11
    elif product_code == 'HRP' and quality == 'PRIME':
        return 1  # ← SPECIAL: UAE HRP PRIME uses Formula 1!
```

#### **Default:**
```python
return 1  # Default to Formula 1 if no match
```

---

## 📐 FORMULA CALCULATIONS

### **Formula 1: China/UAE PRIME (HRP, CRP, PPGI, GPP, GLP, GP)**
```
Average = (Low + High) / 2
LME = Average × 0.95 + 35
```
- Discount: **-5%** (multiply by 0.95)
- Freight: **+35 USD**
- Symbols: MB-STE-0144 (HRP), MB-STE-0145 (CRP), MB-STE-0009 (PPGI/GPP/GLP/GP)

### **Formula 2: China SECONDARY (HRS, CRS, GPS, GLS, PPGIS)**
```
Average = (Low + High) / 2
LME = Average × 0.85 + 45
```
- Discount: **-15%** (multiply by 0.85)
- Freight: **+45 USD**
- Symbols: MB-STE-0144 (HRS), MB-STE-0145 (CRS), MB-STE-0009 (GPS/GLS/PPGIS)

### **Formula 3: China SECONDARY (CRNGO only)**
```
Average = (Low + High) / 2
Step1 = Average × 1.05
LME = Step1 × 0.85 + 45
```
- First: **+5%** (multiply by 1.05)
- Then: **-15%** (multiply by 0.85)
- Freight: **+45 USD**
- Symbol: MB-STE-0145

### **Formula 4: China PRIME (WRLC - Wire Rod Low Carbon)**
```
Average = (Low + High) / 2
LME = Average × 1.05 + 35
```
- Premium: **+5%** (multiply by 1.05)
- Freight: **+35 USD**
- Symbol: MB-STE-0148

### **Formula 5: China PRIME (WRHC - Wire Rod High Carbon)**
```
Average = (Low + High) / 2
LME = Average × 1.05 + 101
```
- Premium: **+5%** (multiply by 1.05)
- Freight: **+101 USD** (+35 base + 66 extra for high carbon)
- Symbol: MB-STE-0148

### **Formula 6: Europe SECONDARY (CRS, GPS)**
```
North_Avg = (North_Low + North_High) / 2
South_Avg = (South_Low + South_High) / 2
Combined_EUR = (North_Avg + South_Avg) / 2
Combined_USD = Combined_EUR ÷ (USD_Rate / EUR_Rate)  ← CRITICAL: DIVIDE!
LME = Combined_USD × 0.85 + 100
```
- Discount: **-15%** (multiply by 0.85)
- Freight: **+100 USD**
- Currency: EUR → USD conversion using **DIVISION**
- Symbols: MB-STE-0026/0027 (CRS), MB-STE-0030/0031 (GPS)

**⚠️ EUR/USD Conversion:** Must use **DIVIDE**, not multiply!

### **Formula 7: Europe SECONDARY (HRS - 3 sources)**
```
North_Avg = (North_Low + North_High) / 2
Italy_Avg = (Italy_Low + Italy_High) / 2
Spain_Avg = (Spain_Low + Spain_High) / 2
Combined_EUR = (North_Avg + Italy_Avg + Spain_Avg) / 3
Combined_USD = Combined_EUR ÷ (USD_Rate / EUR_Rate)
LME = Combined_USD × 0.85 + 100
```
- Discount: **-15%**
- Freight: **+100 USD**
- Symbols: MB-STE-0028 (North), MB-STE-0892 (Italy), MB-STE-0893 (Spain)

### **Formula 8: S.Africa/Taiwan SECONDARY (CRS, GPS - 4 regions)**
```
Europe_USD = convert((North + South) / 2)
Four_Region_Avg = (Europe_USD + UAE_Avg + USA_Avg + China_Avg) / 4
LME = Four_Region_Avg × 0.85 + 100
```
- Discount: **-15%**
- Freight: **+100 USD**
- 4 Regions: Europe, UAE, USA, China
- CRS Symbols: MB-STE-0026/0027 (Europe), MB-STE-0124 (UAE), MB-STE-0181 (USA), MB-STE-0145 (China)
- GPS Symbols: MB-STE-0030/0031 (Europe), MB-STE-0123 (UAE), MB-STE-0182 (USA), MB-STE-0009 (China)

### **Formula 9: S.Africa/Taiwan SECONDARY (HRS - 6 sources)**
```
Europe_USD = convert((North + Italy + Spain) / 3)
Four_Region_Avg = (Europe_USD + CIS_Avg + UAE_Avg + China_Avg) / 4
LME = Four_Region_Avg × 0.85 + 100
```
- Discount: **-15%**
- Freight: **+100 USD**
- 6 Sources: North Europe, Italy, Spain, CIS, UAE, China
- Symbols: MB-STE-0028/0892/0893 (Europe), MB-STE-0014 (CIS), MB-STE-0125 (UAE), MB-STE-0144 (China)

### **Formula 10: UAE/Iran PRIME (WRLC - 5 sources)**
```
Europe_USD = convert((North + South) / 2)
Four_Region_Avg = (Europe_USD + CIS_Avg + Turkish_Avg + China_Avg) / 4
LME = Four_Region_Avg × 1.05 + 35
```
- Premium: **+5%**
- Freight: **+35 USD**
- 5 Sources: North/South Europe, CIS, Turkish, China
- Symbols: MB-STE-0053/0054 (Europe), MB-STE-0017 (CIS), MB-STE-0120 (Turkish), MB-STE-0148 (China)

### **Formula 11: UAE/Iran PRIME (WRHC - 5 sources)**
```
Europe_USD = convert((North + South) / 2)
Four_Region_Avg = (Europe_USD + CIS_Avg + Turkish_Avg + China_Avg) / 4
LME = Four_Region_Avg × 1.05 + 101
```
- Premium: **+5%**
- Freight: **+101 USD** (+35 base + 66 extra for high carbon)
- 5 Sources: North/South Europe, CIS, Turkish, China
- Symbols: same as Formula 10

---

## 🗂️ FILE STRUCTURE

### **Backend Services:**

#### **`backend/services/formula_engine.py`**
- **Purpose:** Formula matching and basic calculations
- **Key Method:** `FormulaEngine.determine_formula(product_code, origin, quality)`
- **Status:** Single source of truth for formula matching
- **Note:** Also contains formula_1 through formula_11 methods. The EUR/USD conversion in formulas 6–11 correctly uses DIVIDE. The standalone `eur_to_usd()` helper uses multiply but is not called by the calculation flow.

#### **`backend/services/lme_calculator.py`**
- **Purpose:** Active calculation engine with database integration
- **Key Methods:**
  - `match_formula()` — Calls FormulaEngine.determine_formula()
  - `get_symbols_for_product()` — Product-specific symbol routing
  - `calculate_formula_1()` through `calculate_formula_11()` — Active calculations
  - `calculate_single_lc(db, lc_id)` — Main calculation entry point
- **Status:** This is the ACTIVE calculation engine
- **EUR Conversion:** Uses DIVIDE (correct method)
- **⚠️ Known bug in `calculate_single_lc`:** Query uses `lm.shipment_date` (should be `lm.last_ship_date`) and `lm.lc_unit_price` (should be `lp.lc_unit_price`)

#### **`backend/services/auth_service.py`**
- **Purpose:** Authentication logic
- **Key Class:** `AuthService`
- **Methods:** Password hashing, JWT token creation/decoding, user authentication, permission checking
- **Note:** Does NOT contain `get_current_user` function

#### **`backend/services/pdf_price_extractor.py`**
- **Purpose:** PDF bulletin parsing
- **Key Features:** 21 FastMarkets symbol definitions, date extraction, price range detection, region mapping

### **Backend API Endpoints:**

#### **`backend/api/auth_endpoints.py`**
- **Purpose:** Authentication endpoints
- **Router Prefix:** `/api/auth`
- **Key Function:** `get_current_user()` — FastAPI dependency for auth
- **Important:** This is where `get_current_user` lives, NOT in auth_service.py

#### **`backend/api/lc_table_endpoints.py`**
- **Purpose:** LC management table endpoints
- **Router Prefix:** `/api/lc-table`
- **Key Endpoint:** `GET /api/lc-table/list` — Returns LC table data with pagination, filtering, sorting
- **Uses:** SQLAlchemy ORM models (LCMaster, LCProduct)
- **Auth Import:** `from api.auth_endpoints import get_current_user`

#### **`backend/api/lme_calculation_endpoints.py`**
- **Purpose:** LME calculation page endpoints
- **Router Prefix:** `/api/calculate`
- **Key Endpoints:**
  - `GET /api/calculate/lcs-for-calculation` — Get LCs for calculation
  - `POST /api/calculate/calculate/{lc_id}` — Calculate single LC
  - `POST /api/calculate/test-match` — Test formula matching
  - `POST /api/calculate/calculate-all` — Calculate all LCs
- **Uses:** Raw SQL queries with text()
- **Auth Import:** `from api.auth_endpoints import get_current_user`

#### **`backend/api/alert_endpoints.py`**
- **Purpose:** Price alert management
- **Router Prefix:** `/api/alerts`
- **Key Endpoints:** GET /list, GET /savings-opportunities, POST /{id}/mark-viewed, POST /{id}/take-action, GET /stats/summary

#### **`backend/api/pdf_upload_endpoints.py`**
- **Purpose:** PDF bulletin upload and extraction
- **Router Prefix:** `/api/pdf`
- **Key Endpoints:** POST /check-rates, POST /upload-bulletin, GET /bulletins/list, DELETE /bulletins/{id}

#### **`backend/api/currency_rate_endpoints.py`**
- **Purpose:** Exchange rate management
- **Router Prefix:** `/api/currency`
- **Key Endpoints:** GET /rates/list, GET /rates/latest, POST /rates/create, PUT /rates/{id}, DELETE /rates/{id}

#### **`backend/api/upload_endpoints.py`**
- **Purpose:** Excel LC file import
- **Router Prefix:** `/api/upload`
- **Key Endpoints:** POST /analyze-lc-file, POST /import-lc-file

#### **`backend/api/lc_endpoints.py`**
- **Status:** Exists but is **commented out** in main.py (not registered)

### **Backend Models:**

#### **`backend/models/database_models.py`**
- **Purpose:** SQLAlchemy ORM models
- **Classes:** Role, User, UserSession, CalculationFormula, LCMaster, LCProduct, LMEBulletin, LMEPrice, PriceAlert, WhatsAppConfig, AuditLog, LMEPriceHistory, CurrencyRate

### **Backend Config:**

#### **`backend/config/database.py`**
- **Purpose:** Database connection setup
- **Function:** `get_db()` — FastAPI dependency for DB sessions

---

## 🔧 CORRECT IMPORT PATTERNS

### **Authentication:**
```python
from api.auth_endpoints import get_current_user  # ✅ CORRECT
from models.database_models import User

# In endpoint:
async def my_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
```

**❌ WRONG:**
```python
from services.auth_service import get_current_user  # ❌ WRONG - doesn't exist there
```

### **Formula Matching:**
```python
from services.formula_engine import FormulaEngine

formula_number = FormulaEngine.determine_formula(product_code, origin, quality)
```

### **LME Calculation:**
```python
from services.lme_calculator import LMECalculator

result = LMECalculator.calculate_single_lc(db, lc_id)
```

### **Database:**
```python
from config.database import get_db
from sqlalchemy import text

# For raw SQL:
query = text("SELECT * FROM lc_master WHERE lc_id = :lc_id")
result = db.execute(query, {"lc_id": lc_id}).fetchall()
```

---

## 🐛 KNOWN BUGS AND FIXES

### **1. Netherlands Bug (✅ FIXED)**
**Problem:** Netherlands LCs showing Formula 1 instead of Formula 6

**Root Cause:** Europe origin check missing 'NETHERLAND'

**Fix applied in `formula_engine.py`:**
```python
elif ('EUROPE' in origin_upper or
      'GERMAN' in origin_upper or
      'ITALY' in origin_upper or
      'SPAIN' in origin_upper or
      'NETHERLAND' in origin_upper):  # ← ADDED
```

### **2. Formula 4 Bug (✅ FIXED)**
**Problem:** Formula 4 calculation was calling `calculate_formula_5` instead of `calculate_formula_4`

**Fix applied in `lme_calculator.py` line ~515:**
```python
elif formula_number == 4:
    lme_value = LMECalculator.calculate_formula_4(prices, product_code)  # ✅ FIXED
```

### **3. Table Prefix Bug in lme_calculation_endpoints.py (✅ FIXED)**
**Problem:** `/lcs-for-calculation` query used `lm.current_lme` and `lm.lc_unit_price` — columns that don't exist in `lc_master`

**Fix applied in `lme_calculation_endpoints.py`:**
```python
# WRONG (old):
lm.current_lme,
lm.lc_unit_price,

# CORRECT (fixed):
lp.current_lme,
lp.lc_unit_price,
```

### **4. EUR/USD Conversion Direction**
**Problem:** Some code multiplies instead of divides for EUR→USD conversion

**Status:**
- `lme_calculator.py` formulas 6–11: ✅ CORRECT (divide)
- `formula_engine.py` formula methods 6–11: ✅ CORRECT (divide)
- `formula_engine.py` standalone `eur_to_usd()` helper: ❌ Uses multiply — but this method is NOT called by the active calculation flow

**Correct Method:**
```python
# CORRECT:
combined_usd = combined_eur / (usd_rate / eur_rate)  # ✅ DIVIDE
```

### **5. calculate_single_lc Query Bug (⚠️ ACTIVE)**
**Location:** `backend/services/lme_calculator.py` in `calculate_single_lc()`

**Problem:** SQL query uses wrong column names from lc_master:
```python
# WRONG (current code):
SELECT lm.lc_id, lm.lc_number, lm.lc_date, lm.shipment_date, lm.lc_unit_price, ...

# CORRECT (should be):
SELECT lm.lc_id, lm.lc_number, lm.lc_date, lm.last_ship_date, lp.lc_unit_price, ...
```
- `lm.shipment_date` → correct column name is `lm.last_ship_date`
- `lm.lc_unit_price` → column is in `lp` (lc_products), not `lm` (lc_master)

---

## 🔍 CORRECT SQL QUERIES

### **Get LCs for Calculation Page:**
```sql
SELECT
    lm.lc_id,
    lm.lc_number,
    lp.current_lme,        -- ← lp. not lm.
    lp.lc_unit_price,      -- ← lp. not lm.
    lp.product_code,
    lp.origin,
    lp.quality
FROM lc_master lm
LEFT JOIN lc_products lp ON lm.lc_id = lp.lc_id
WHERE lm.status != 'CLOSED'
ORDER BY lm.lc_date DESC
```

### **Get LC Details:**
```sql
SELECT
    lm.*,
    lp.product_code,
    lp.origin,
    lp.quality,
    lp.lc_unit_price,
    lp.current_lme
FROM lc_master lm
LEFT JOIN lc_products lp ON lm.lc_id = lp.lc_id
WHERE lm.lc_id = :lc_id
```

### **Get Bulletin Prices:**
```sql
SELECT fastmarket_symbol, low_price, high_price
FROM lme_prices
WHERE bulletin_id = :bulletin_id
AND fastmarket_symbol IN (:symbol1, :symbol2, ...)
```
**⚠️ Column names:** `fastmarket_symbol`, `low_price`, `high_price` — NOT `symbol`, `price_low`, `price_high`!

### **Get Currency Rates:**
```sql
SELECT usd_rate, eur_rate
FROM currency_rates
WHERE rate_id = (
    SELECT rate_id FROM lme_bulletins WHERE bulletin_id = :bulletin_id
)
```
**⚠️ Rates are in `currency_rates` table, NOT in `lme_bulletins`!**

### **Update LC with Calculated LME:**
```sql
UPDATE lc_products
SET current_lme = :lme_value,
    last_lme_update = CURRENT_TIMESTAMP
WHERE lc_id = :lc_id
```
**⚠️ Update `lc_products.current_lme`, not `lc_master`!**

---

## 📊 API ENDPOINT REFERENCE

### **Authentication Endpoints (`/api/auth`)**
- `POST /api/auth/login` — User login (returns JWT + refresh token)
- `POST /api/auth/logout` — Session invalidation
- `GET /api/auth/me` — Current user info
- `POST /api/auth/change-password` — Change password
- `GET /api/auth/check-permission/{permission}` — Check user permission
- `POST /api/auth/refresh` — Refresh access token

### **LC Table Endpoints (`/api/lc-table`)**
- `GET /api/lc-table/list` — Get LC table data
  - Query params: `page`, `page_size`, `status`, `search`, `product_code`, `origin`, `quality`, `date_from`, `date_to`, `sort_by`, `sort_order`
- `GET /api/lc-table/filter-options` — Get filter options
- `DELETE /api/lc-table/{lc_id}` — Delete LC (requires `delete_lc` permission)

### **LME Calculation Endpoints (`/api/calculate`)**
- `GET /api/calculate/lcs-for-calculation` — Get LCs for calculation page
- `POST /api/calculate/calculate/{lc_id}` — Calculate single LC
- `POST /api/calculate/test-match` — Test formula matching
- `POST /api/calculate/calculate-all` — Calculate all active LCs

### **Alert Endpoints (`/api/alerts`)**
- `GET /api/alerts/list` — Alert listing with filters
- `GET /api/alerts/savings-opportunities` — Cost savings alerts
- `POST /api/alerts/{alert_id}/mark-viewed` — Mark alert viewed
- `POST /api/alerts/{alert_id}/take-action` — Record action
- `GET /api/alerts/stats/summary` — Alert statistics

### **PDF Upload Endpoints (`/api/pdf`)**
- `POST /api/pdf/check-rates` — Pre-upload currency check
- `POST /api/pdf/upload-bulletin` — Upload and extract PDF
- `GET /api/pdf/bulletins/list` — Bulletin history
- `DELETE /api/pdf/bulletins/{bulletin_id}` — Delete bulletin

### **Currency Rate Endpoints (`/api/currency`)**
- `GET /api/currency/rates/list` — Currency rates history
- `GET /api/currency/rates/latest` — Latest rate
- `GET /api/currency/rates/date/{rate_date}` — Rate by date
- `POST /api/currency/rates/create` — New rate creation
- `PUT /api/currency/rates/{rate_id}` — Rate update
- `DELETE /api/currency/rates/{rate_id}` — Rate deletion

### **Upload Endpoints (`/api/upload`)**
- `POST /api/upload/analyze-lc-file` — Analyze Excel LC file
- `POST /api/upload/import-lc-file` — Import LC from Excel

---

## ⚠️ CRITICAL REMINDERS

### **DO NOT:**
- ❌ Use `lm.current_lme` or `lm.lc_unit_price` (wrong table)
- ❌ Import `get_current_user` from `auth_service`
- ❌ Multiply for EUR→USD conversion
- ❌ Forget 'NETHERLAND' in Europe origin check
- ❌ Use `symbol`, `price_low`, `price_high` in lme_prices queries
- ❌ Query `usd_rate`/`eur_rate` from `lme_bulletins` (not there — use `currency_rates`)
- ❌ Use status values ACTIVE/MONITORING/EXPIRING/REOPENED (use OPEN/CLOSED/SHIPPED/EXPIRED/CANCELLED)

### **ALWAYS:**
- ✅ Use `lp.current_lme` and `lp.lc_unit_price` (correct table)
- ✅ Import `get_current_user` from `auth_endpoints`
- ✅ Divide for EUR→USD conversion
- ✅ Include 'NETHERLAND' in Europe check
- ✅ Use `fastmarket_symbol`, `low_price`, `high_price` in lme_prices queries
- ✅ Get currency rates from `currency_rates` table via `rate_id` FK
- ✅ Ask before creating any file
- ✅ Check this reference doc before coding

---

## 🎯 TESTING CHECKLIST

### **After Any Changes:**
1. ✅ Clear cache: `rmdir /s /q __pycache__ services\__pycache__ api\__pycache__`
2. ✅ Restart server: `python main.py`
3. ✅ Test Netherlands LC: Should show Formula 6
4. ✅ Test calculation page: Should load LC data
5. ✅ Test formula matching: `python services/formula_engine.py`

### **Expected Results:**
- Netherlands CRS SECONDARY → Formula 6 ✅
- Calculate LME page loads LC list ✅
- Can calculate LME for any LC ✅
- No auth import errors ✅
- No table column errors ✅

---

## 📌 QUICK REFERENCE

**Database:** `lp.current_lme`, `lp.lc_unit_price` (NOT lm.)
**Auth Import:** `from api.auth_endpoints import get_current_user`
**Formula Match:** `FormulaEngine.determine_formula(product_code, origin, quality)`
**Calculate:** `LMECalculator.calculate_single_lc(db, lc_id)`
**Netherlands:** Must include in Europe check
**EUR→USD:** Use division, not multiplication
**lme_prices columns:** `fastmarket_symbol`, `low_price`, `high_price`
**Currency rates:** In `currency_rates` table, NOT `lme_bulletins`
**LC status values:** OPEN, CLOSED, SHIPPED, EXPIRED, CANCELLED

---

**END OF REFERENCE DOCUMENT**
