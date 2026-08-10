-- ================================================================
-- LME MONITORING SYSTEM - DATABASE SCHEMA
-- Version: 1.0
-- Date: February 23, 2026
-- Purpose: Track LC prices vs LME prices with WhatsApp alerts
-- ================================================================

-- Drop existing tables (for clean setup)
DROP TABLE IF EXISTS price_alerts CASCADE;
DROP TABLE IF EXISTS whatsapp_config CASCADE;
DROP TABLE IF EXISTS lme_prices CASCADE;
DROP TABLE IF EXISTS lme_bulletins CASCADE;
DROP TABLE IF EXISTS lc_products CASCADE;
DROP TABLE IF EXISTS lc_master CASCADE;
DROP TABLE IF EXISTS calculation_formulas CASCADE;

-- ================================================================
-- TABLE 1: calculation_formulas
-- Purpose: Store all 11 LME calculation formulas
-- ================================================================
CREATE TABLE calculation_formulas (
    formula_id SERIAL PRIMARY KEY,
    formula_number INTEGER NOT NULL,
    origin VARCHAR(50) NOT NULL,
    region VARCHAR(10) NOT NULL,
    quality VARCHAR(20) NOT NULL,
    products TEXT[], -- Array of product codes
    discount_type VARCHAR(10), -- 'ADD' or 'SUBTRACT'
    discount_percent DECIMAL(5,2),
    freight_base DECIMAL(10,2),
    freight_additional DECIMAL(10,2) DEFAULT 0,
    eur_usd_conversion BOOLEAN DEFAULT FALSE,
    multi_source BOOLEAN DEFAULT FALSE,
    formula_description TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert all 11 formulas (CORRECTED versions)
INSERT INTO calculation_formulas (formula_number, origin, region, quality, products, discount_type, discount_percent, freight_base, freight_additional, eur_usd_conversion, formula_description, notes) VALUES
(1, 'CHINA', '1', 'PRIME', ARRAY['HRP','CRP','PPGI','GPP','GLP','GP'], 'SUBTRACT', 5.00, 35, 0, FALSE, 'LME = Average × 0.95 + 35', 'CORRECTED: Discount is -5%, UAE HRP also uses this'),
(2, 'CHINA', '1', 'SECONDARY', ARRAY['HRS','CRS','GPS','GLS','PPGIS'], 'SUBTRACT', 15.00, 45, 0, FALSE, 'LME = Average × 0.85 + 45', NULL),
(3, 'CHINA', '1', 'SECONDARY', ARRAY['CRNGO'], 'SUBTRACT', 15.00, 45, 0, FALSE, 'LME = Average × 1.05 × 0.85 + 45', 'CORRECTED: Add 5% first, then subtract 15%'),
(4, 'CHINA', '1', 'PRIME', ARRAY['WRLC'], 'ADD', 5.00, 35, 0, FALSE, 'LME = Average × 1.05 + 35', NULL),
(5, 'CHINA', '1', 'PRIME', ARRAY['WRHC'], 'ADD', 5.00, 35, 66, FALSE, 'LME = Average × 1.05 + 101', 'Extra +66 freight for High Carbon'),
(6, 'EUROPE', '2', 'SECONDARY', ARRAY['CRS','GPS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (Combined × USD/EUR) × 0.85 + 100', 'CORRECTED: Uses USD/EUR not EUR/USD'),
(7, 'EUROPE', '2A', 'SECONDARY', ARRAY['HRS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = ((3-source avg) × USD/EUR) × 0.85 + 100', '3 sources: North Europe, Italy, Spain'),
(8, 'SOUTH AFRICA', '4A', 'SECONDARY', ARRAY['CRS','GPS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (4-source avg) × 0.85 + 100', '4 sources: Europe, UAE, US, China'),
(9, 'SOUTH AFRICA', '4B', 'SECONDARY', ARRAY['HRS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (6-source avg) × 0.85 + 100', '6 sources: N.EU, Italy, Spain, CIS, UAE, China'),
(10, 'UAE', '4C', 'PRIME', ARRAY['WRLC'], 'ADD', 5.00, 35, 0, TRUE, 'LME = (Total_Avg × 1.05) + 35', 'From Excel: Europe, CIS, Turkish, China'),
(11, 'UAE', '4C', 'PRIME', ARRAY['WRHC'], 'ADD', 5.00, 35, 66, TRUE, 'LME = (Total_Avg × 1.05) + 101', 'Extra +66 freight for High Carbon');

-- Also add TAIWAN and IRAN (same as S.AFRICA and UAE)
INSERT INTO calculation_formulas (formula_number, origin, region, quality, products, discount_type, discount_percent, freight_base, freight_additional, eur_usd_conversion, formula_description, notes) VALUES
(8, 'TAIWAN', '4A', 'SECONDARY', ARRAY['CRS','GPS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (4-source avg) × 0.85 + 100', 'Same as South Africa'),
(9, 'TAIWAN', '4B', 'SECONDARY', ARRAY['HRS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (6-source avg) × 0.85 + 100', 'Same as South Africa'),
(10, 'IRAN', '4C', 'PRIME', ARRAY['WRLC'], 'ADD', 5.00, 35, 0, TRUE, 'LME = (Total_Avg × 1.05) + 35', 'Same as UAE'),
(11, 'IRAN', '4C', 'PRIME', ARRAY['WRHC'], 'ADD', 5.00, 35, 66, TRUE, 'LME = (Total_Avg × 1.05) + 101', 'Same as UAE');

-- Special exception for UAE HRP
INSERT INTO calculation_formulas (formula_number, origin, region, quality, products, discount_type, discount_percent, freight_base, freight_additional, eur_usd_conversion, formula_description, notes) VALUES
(1, 'UAE', '1', 'PRIME', ARRAY['HRP'], 'SUBTRACT', 5.00, 35, 0, FALSE, 'LME = Average × 0.95 + 35', 'EXCEPTION: UAE HRP uses China Formula 1');

-- ================================================================
-- TABLE 2: lc_master
-- Purpose: Main LC information imported from old software
-- ================================================================
CREATE TABLE lc_master (
    lc_id SERIAL PRIMARY KEY,
    lc_number VARCHAR(50) UNIQUE NOT NULL,
    contract_number VARCHAR(50),
    supplier_name VARCHAR(200),
    importer_name VARCHAR(200),
    lc_date DATE NOT NULL,
    expiry_date DATE,
    last_ship_date DATE,
    lc_amount DECIMAL(15,2),
    currency VARCHAR(10),
    hoa VARCHAR(100), -- House of Account
    payment_terms VARCHAR(200),
    booked_by VARCHAR(100),
    
    -- Monitoring fields
    monitoring_expiry DATE NOT NULL, -- LC Date + 40 days
    days_remaining INTEGER GENERATED ALWAYS AS (monitoring_expiry - CURRENT_DATE) STORED,
    status VARCHAR(20) DEFAULT 'ACTIVE', -- ACTIVE, EXPIRED, REOPENED, CLOSED
    
    -- Timestamps
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- Constraints
    CONSTRAINT valid_status CHECK (status IN ('ACTIVE', 'EXPIRED', 'REOPENED', 'CLOSED'))
);

-- Index for faster queries
CREATE INDEX idx_lc_status ON lc_master(status);
CREATE INDEX idx_lc_monitoring_expiry ON lc_master(monitoring_expiry);
CREATE INDEX idx_lc_date ON lc_master(lc_date);

-- ================================================================
-- TABLE 3: lc_products
-- Purpose: Product line items for each LC
-- ================================================================
CREATE TABLE lc_products (
    line_id SERIAL PRIMARY KEY,
    lc_id INTEGER NOT NULL REFERENCES lc_master(lc_id) ON DELETE CASCADE,
    
    -- Product details
    product_code VARCHAR(20) NOT NULL,
    product_name VARCHAR(100),
    origin VARCHAR(50) NOT NULL,
    quality VARCHAR(20) NOT NULL,
    cargo_nature VARCHAR(20),
    
    -- Quantity and pricing
    quantity DECIMAL(12,2) NOT NULL,
    unit VARCHAR(10) DEFAULT 'MT',
    lc_unit_price DECIMAL(10,2) NOT NULL,
    
    -- LME comparison
    current_lme_price DECIMAL(10,2),
    price_difference DECIMAL(10,2) GENERATED ALWAYS AS (current_lme_price - lc_unit_price) STORED,
    price_diff_percent DECIMAL(5,2) GENERATED ALWAYS AS (
        CASE 
            WHEN lc_unit_price > 0 THEN ((current_lme_price - lc_unit_price) / lc_unit_price) * 100
            ELSE 0
        END
    ) STORED,
    
    -- Additional info
    hs_code VARCHAR(20),
    grade VARCHAR(50),
    size VARCHAR(50),
    
    -- Tracking
    last_lme_update TIMESTAMP,
    change_detected BOOLEAN DEFAULT FALSE,
    
    CONSTRAINT valid_quality CHECK (quality IN ('PRIME', 'SECONDARY', 'PRM', 'SEC'))
);

-- Indexes
CREATE INDEX idx_product_lc ON lc_products(lc_id);
CREATE INDEX idx_product_code ON lc_products(product_code);
CREATE INDEX idx_product_origin ON lc_products(origin);
CREATE INDEX idx_change_detected ON lc_products(change_detected);

-- ================================================================
-- TABLE 4: lme_bulletins
-- Purpose: Track uploaded FastMarkets PDFs
-- ================================================================
CREATE TABLE lme_bulletins (
    bulletin_id SERIAL PRIMARY KEY,
    bulletin_date DATE NOT NULL,
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    uploaded_by VARCHAR(100),
    
    -- Processing status
    processed BOOLEAN DEFAULT FALSE,
    processing_started TIMESTAMP,
    processing_completed TIMESTAMP,
    processing_errors TEXT,
    
    -- Extracted data (JSON format)
    extracted_data JSONB,
    total_prices_extracted INTEGER,
    
    -- Constraints
    UNIQUE(bulletin_date, file_name)
);

-- Index
CREATE INDEX idx_bulletin_date ON lme_bulletins(bulletin_date DESC);
CREATE INDEX idx_bulletin_processed ON lme_bulletins(processed);

-- ================================================================
-- TABLE 5: lme_prices
-- Purpose: Store extracted and calculated LME prices
-- ================================================================
CREATE TABLE lme_prices (
    price_id SERIAL PRIMARY KEY,
    bulletin_id INTEGER REFERENCES lme_bulletins(bulletin_id) ON DELETE CASCADE,
    
    -- Price source
    fastmarket_symbol VARCHAR(50),
    product_code VARCHAR(20) NOT NULL,
    origin VARCHAR(50) NOT NULL,
    quality VARCHAR(20),
    
    -- Raw prices
    low_price DECIMAL(10,2),
    high_price DECIMAL(10,2),
    average_price DECIMAL(10,2) GENERATED ALWAYS AS ((low_price + high_price) / 2) STORED,
    
    -- Calculated LME
    calculated_lme DECIMAL(10,2),
    formula_used VARCHAR(20),
    calculation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- EUR/USD conversion (if applicable)
    eur_rate DECIMAL(10,6),
    usd_rate DECIMAL(10,6),
    conversion_rate DECIMAL(10,6),
    
    -- Additional metadata
    notes TEXT,
    
    CONSTRAINT valid_prices CHECK (low_price <= high_price)
);

-- Indexes
CREATE INDEX idx_lme_bulletin ON lme_prices(bulletin_id);
CREATE INDEX idx_lme_product ON lme_prices(product_code, origin, quality);
CREATE INDEX idx_lme_date ON lme_prices(calculation_date DESC);

-- ================================================================
-- TABLE 6: price_alerts
-- Purpose: Store alerts when LME prices change
-- ================================================================
CREATE TABLE price_alerts (
    alert_id SERIAL PRIMARY KEY,
    lc_id INTEGER NOT NULL REFERENCES lc_master(lc_id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES lc_products(line_id) ON DELETE CASCADE,
    
    -- Alert details
    alert_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    alert_type VARCHAR(20) NOT NULL, -- DECREASE, INCREASE
    priority VARCHAR(20) DEFAULT 'MEDIUM', -- HIGH, MEDIUM, LOW
    
    -- Price information
    old_lme_price DECIMAL(10,2),
    new_lme_price DECIMAL(10,2),
    lc_price DECIMAL(10,2),
    difference DECIMAL(10,2),
    difference_percent DECIMAL(5,2),
    
    -- Potential impact
    quantity DECIMAL(12,2),
    total_impact DECIMAL(15,2) GENERATED ALWAYS AS (difference * quantity) STORED,
    
    -- Alert delivery
    whatsapp_sent BOOLEAN DEFAULT FALSE,
    whatsapp_sent_at TIMESTAMP,
    whatsapp_delivery_status VARCHAR(50),
    whatsapp_message_id VARCHAR(100),
    
    -- Dashboard tracking
    viewed BOOLEAN DEFAULT FALSE,
    viewed_at TIMESTAMP,
    viewed_by VARCHAR(100),
    
    -- Action taken
    action_taken VARCHAR(50), -- REOPENED, IGNORED, NOTED
    action_date TIMESTAMP,
    action_by VARCHAR(100),
    action_notes TEXT,
    
    CONSTRAINT valid_alert_type CHECK (alert_type IN ('DECREASE', 'INCREASE')),
    CONSTRAINT valid_priority CHECK (priority IN ('HIGH', 'MEDIUM', 'LOW'))
);

-- Indexes
CREATE INDEX idx_alert_lc ON price_alerts(lc_id);
CREATE INDEX idx_alert_type ON price_alerts(alert_type);
CREATE INDEX idx_alert_date ON price_alerts(alert_date DESC);
CREATE INDEX idx_alert_viewed ON price_alerts(viewed);
CREATE INDEX idx_whatsapp_sent ON price_alerts(whatsapp_sent);

-- ================================================================
-- TABLE 7: whatsapp_config
-- Purpose: WhatsApp recipients and settings
-- ================================================================
CREATE TABLE whatsapp_config (
    config_id SERIAL PRIMARY KEY,
    recipient_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) NOT NULL UNIQUE,
    country_code VARCHAR(5) DEFAULT '+92',
    
    -- Alert preferences
    alert_type VARCHAR(50) DEFAULT 'ALL', -- ALL, DECREASE_ONLY, CRITICAL_ONLY
    min_amount_threshold DECIMAL(10,2), -- Only alert if impact > this amount
    min_percent_threshold DECIMAL(5,2), -- Only alert if change > this %
    
    -- Status
    active BOOLEAN DEFAULT TRUE,
    last_message_sent TIMESTAMP,
    total_messages_sent INTEGER DEFAULT 0,
    
    -- Settings
    send_daily_summary BOOLEAN DEFAULT FALSE,
    send_weekly_report BOOLEAN DEFAULT FALSE,
    quiet_hours_start TIME,
    quiet_hours_end TIME,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- VIEWS FOR EASY QUERYING
-- ================================================================

-- View 1: Active LCs with current status
CREATE OR REPLACE VIEW vw_active_lcs AS
SELECT 
    lm.lc_id,
    lm.lc_number,
    lm.supplier_name,
    lm.lc_date,
    lm.monitoring_expiry,
    lm.days_remaining,
    lm.status,
    COUNT(lp.line_id) as total_products,
    COUNT(CASE WHEN lp.change_detected THEN 1 END) as products_with_changes,
    SUM(lp.quantity * lp.lc_unit_price) as total_lc_value,
    SUM(lp.quantity * COALESCE(lp.price_difference, 0)) as total_potential_impact
FROM lc_master lm
LEFT JOIN lc_products lp ON lm.lc_id = lp.lc_id
WHERE lm.status = 'ACTIVE'
GROUP BY lm.lc_id, lm.lc_number, lm.supplier_name, lm.lc_date, 
         lm.monitoring_expiry, lm.days_remaining, lm.status;

-- View 2: Alert dashboard
CREATE OR REPLACE VIEW vw_alert_dashboard AS
SELECT 
    pa.alert_id,
    pa.alert_date,
    pa.alert_type,
    pa.priority,
    lm.lc_number,
    lm.supplier_name,
    lm.days_remaining,
    lp.product_code,
    lp.origin,
    pa.lc_price,
    pa.old_lme_price,
    pa.new_lme_price,
    pa.difference,
    pa.difference_percent,
    pa.quantity,
    pa.total_impact,
    pa.whatsapp_sent,
    pa.viewed,
    pa.action_taken
FROM price_alerts pa
JOIN lc_master lm ON pa.lc_id = lm.lc_id
JOIN lc_products lp ON pa.line_id = lp.line_id
ORDER BY pa.alert_date DESC;

-- View 3: Savings opportunities (LME decreased)
CREATE OR REPLACE VIEW vw_savings_opportunities AS
SELECT 
    lm.lc_number,
    lm.supplier_name,
    lm.days_remaining,
    lp.product_code,
    lp.origin,
    lp.quality,
    lp.quantity,
    lp.lc_unit_price,
    lp.current_lme_price,
    lp.price_difference,
    lp.price_diff_percent,
    (lp.quantity * ABS(lp.price_difference)) as potential_savings
FROM lc_master lm
JOIN lc_products lp ON lm.lc_id = lp.lc_id
WHERE lm.status = 'ACTIVE'
  AND lp.price_difference < 0  -- LME is lower than LC price
  AND lm.days_remaining > 0
ORDER BY potential_savings DESC;

-- View 4: Expiring soon (less than 5 days)
CREATE OR REPLACE VIEW vw_expiring_soon AS
SELECT 
    lc_number,
    supplier_name,
    lc_date,
    monitoring_expiry,
    days_remaining,
    status
FROM lc_master
WHERE status = 'ACTIVE'
  AND days_remaining BETWEEN 0 AND 5
ORDER BY days_remaining ASC;

-- ================================================================
-- FUNCTIONS
-- ================================================================

-- Function to auto-update LC status based on days
CREATE OR REPLACE FUNCTION update_lc_status()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.days_remaining < 0 AND NEW.status = 'ACTIVE' THEN
        NEW.status := 'EXPIRED';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger to update status
CREATE TRIGGER trg_update_lc_status
BEFORE UPDATE ON lc_master
FOR EACH ROW
EXECUTE FUNCTION update_lc_status();

-- Function to update last_updated timestamp
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for lc_master
CREATE TRIGGER trg_lc_master_timestamp
BEFORE UPDATE ON lc_master
FOR EACH ROW
EXECUTE FUNCTION update_timestamp();

-- ================================================================
-- SAMPLE DATA (for testing)
-- ================================================================

-- Insert sample WhatsApp config
INSERT INTO whatsapp_config (recipient_name, phone_number, alert_type, active) VALUES
('Manager - Ahsan', '+923001234567', 'ALL', TRUE),
('Operations Team', '+923007654321', 'DECREASE_ONLY', TRUE);

-- ================================================================
-- USEFUL QUERIES (commented out, for reference)
-- ================================================================

/*
-- Get all active LCs with savings opportunities
SELECT * FROM vw_savings_opportunities;

-- Get unviewed alerts
SELECT * FROM vw_alert_dashboard WHERE viewed = FALSE;

-- Get LCs expiring soon
SELECT * FROM vw_expiring_soon;

-- Get latest LME prices
SELECT 
    product_code,
    origin,
    quality,
    calculated_lme,
    calculation_date
FROM lme_prices
WHERE calculation_date = (SELECT MAX(calculation_date) FROM lme_prices)
ORDER BY product_code, origin;

-- Count alerts by type
SELECT 
    alert_type,
    priority,
    COUNT(*) as total_alerts,
    SUM(CASE WHEN viewed THEN 1 ELSE 0 END) as viewed,
    SUM(CASE WHEN whatsapp_sent THEN 1 ELSE 0 END) as whatsapp_sent
FROM price_alerts
WHERE alert_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY alert_type, priority;
*/

-- ================================================================
-- PERMISSIONS (optional - for multi-user setup)
-- ================================================================

-- Create roles (uncomment if needed)
-- CREATE ROLE lme_admin;
-- CREATE ROLE lme_operator;
-- CREATE ROLE lme_viewer;

-- Grant permissions
-- GRANT ALL ON ALL TABLES IN SCHEMA public TO lme_admin;
-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO lme_operator;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO lme_viewer;

-- ================================================================
-- END OF SCHEMA
-- ================================================================

COMMENT ON DATABASE postgres IS 'LME Monitoring System - Track LC vs LME prices with WhatsApp alerts';
