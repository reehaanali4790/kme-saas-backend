-- LME Monitoring System - PostgreSQL 18 Compatible Schema
-- Version: 2.0

DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS user_sessions CASCADE;
DROP TABLE IF EXISTS price_alerts CASCADE;
DROP TABLE IF EXISTS whatsapp_config CASCADE;
DROP TABLE IF EXISTS lme_prices CASCADE;
DROP TABLE IF EXISTS lme_bulletins CASCADE;
DROP TABLE IF EXISTS lc_products CASCADE;
DROP TABLE IF EXISTS lc_master CASCADE;
DROP TABLE IF EXISTS calculation_formulas CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS roles CASCADE;

-- Roles
CREATE TABLE roles (
    role_id SERIAL PRIMARY KEY,
    role_name VARCHAR(50) UNIQUE NOT NULL,
    role_description TEXT,
    can_import_lc BOOLEAN DEFAULT FALSE,
    can_upload_pdf BOOLEAN DEFAULT FALSE,
    can_view_dashboard BOOLEAN DEFAULT TRUE,
    can_edit_lc BOOLEAN DEFAULT FALSE,
    can_delete_lc BOOLEAN DEFAULT FALSE,
    can_manage_users BOOLEAN DEFAULT FALSE,
    can_configure_alerts BOOLEAN DEFAULT FALSE,
    can_view_all_lcs BOOLEAN DEFAULT TRUE,
    can_export_reports BOOLEAN DEFAULT FALSE,
    can_reopen_lc BOOLEAN DEFAULT FALSE,
    can_change_lc_status BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO roles (role_name, role_description, can_import_lc, can_upload_pdf, can_view_dashboard, can_edit_lc, can_delete_lc, can_manage_users, can_configure_alerts, can_view_all_lcs, can_export_reports, can_reopen_lc, can_change_lc_status) VALUES
('ADMIN', 'Full system access', TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE),
('MANAGER', 'Can manage LCs and view all data', TRUE, TRUE, TRUE, TRUE, FALSE, FALSE, TRUE, TRUE, TRUE, TRUE, TRUE),
('OPERATOR', 'Can upload PDFs and view data', FALSE, TRUE, TRUE, FALSE, FALSE, FALSE, FALSE, TRUE, TRUE, FALSE, FALSE),
('VIEWER', 'Read-only access', FALSE, FALSE, TRUE, FALSE, FALSE, FALSE, FALSE, TRUE, FALSE, FALSE, FALSE);

-- Users
CREATE TABLE users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role_id INTEGER NOT NULL REFERENCES roles(role_id),
    phone_number VARCHAR(20),
    whatsapp_number VARCHAR(20),
    active BOOLEAN DEFAULT TRUE,
    email_verified BOOLEAN DEFAULT FALSE,
    last_login TIMESTAMP,
    login_count INTEGER DEFAULT 0,
    receive_whatsapp BOOLEAN DEFAULT TRUE,
    receive_email BOOLEAN DEFAULT TRUE,
    language VARCHAR(10) DEFAULT 'en',
    timezone VARCHAR(50) DEFAULT 'Asia/Karachi',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES users(user_id),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(user_id)
);

CREATE INDEX idx_users_role ON users(role_id);
CREATE INDEX idx_users_active ON users(active);

-- Default users (password: admin123, manager123, operator123, viewer123)
INSERT INTO users (username, email, password_hash, full_name, role_id, phone_number, whatsapp_number, active) VALUES
('admin', 'admin@lme-system.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYCxGYKP.mK', 'System Administrator', 1, '+923001234567', '+923001234567', TRUE),
('manager1', 'ahsan@lme-system.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYCxGYKP.mK', 'Ahsan (Manager)', 2, '+923009876543', '+923009876543', TRUE),
('operator1', 'operator@lme-system.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYCxGYKP.mK', 'LC Operator', 3, NULL, NULL, TRUE),
('viewer1', 'viewer@lme-system.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYCxGYKP.mK', 'Finance Viewer', 4, NULL, NULL, TRUE);

-- User Sessions
CREATE TABLE user_sessions (
    session_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    session_token VARCHAR(255) UNIQUE NOT NULL,
    ip_address INET,
    user_agent TEXT,
    login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    logout_time TIMESTAMP,
    active BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_sessions_user ON user_sessions(user_id);
CREATE INDEX idx_sessions_active ON user_sessions(active);

-- Calculation Formulas
CREATE TABLE calculation_formulas (
    formula_id SERIAL PRIMARY KEY,
    formula_number INTEGER NOT NULL,
    origin VARCHAR(50) NOT NULL,
    region VARCHAR(10) NOT NULL,
    quality VARCHAR(20) NOT NULL,
    products TEXT[],
    discount_type VARCHAR(10),
    discount_percent NUMERIC(5,2),
    freight_base NUMERIC(10,2),
    freight_additional NUMERIC(10,2) DEFAULT 0,
    eur_usd_conversion BOOLEAN DEFAULT FALSE,
    multi_source BOOLEAN DEFAULT FALSE,
    formula_description TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO calculation_formulas (formula_number, origin, region, quality, products, discount_type, discount_percent, freight_base, freight_additional, eur_usd_conversion, formula_description, notes) VALUES
(1, 'CHINA', '1', 'PRIME', ARRAY['HRP','CRP','PPGI','GPP','GLP','GP'], 'SUBTRACT', 5.00, 35, 0, FALSE, 'LME = Average × 0.95 + 35', 'CORRECTED: -5%'),
(2, 'CHINA', '1', 'SECONDARY', ARRAY['HRS','CRS','GPS','GLS','PPGIS'], 'SUBTRACT', 15.00, 45, 0, FALSE, 'LME = Average × 0.85 + 45', NULL),
(3, 'CHINA', '1', 'SECONDARY', ARRAY['CRNGO'], 'SUBTRACT', 15.00, 45, 0, FALSE, 'LME = Average × 1.05 × 0.85 + 45', 'CORRECTED: +5% then -15%'),
(4, 'CHINA', '1', 'PRIME', ARRAY['WRLC'], 'ADD', 5.00, 35, 0, FALSE, 'LME = Average × 1.05 + 35', NULL),
(5, 'CHINA', '1', 'PRIME', ARRAY['WRHC'], 'ADD', 5.00, 35, 66, FALSE, 'LME = Average × 1.05 + 101', '+66 freight'),
(6, 'EUROPE', '2', 'SECONDARY', ARRAY['CRS','GPS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (Combined × USD/EUR) × 0.85 + 100', 'CORRECTED: USD/EUR'),
(7, 'EUROPE', '2A', 'SECONDARY', ARRAY['HRS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = ((3-source avg) × USD/EUR) × 0.85 + 100', '3 sources'),
(8, 'SOUTH AFRICA', '4A', 'SECONDARY', ARRAY['CRS','GPS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (4-source avg) × 0.85 + 100', '4 sources'),
(9, 'SOUTH AFRICA', '4B', 'SECONDARY', ARRAY['HRS'], 'SUBTRACT', 15.00, 100, 0, TRUE, 'LME = (6-source avg) × 0.85 + 100', '6 sources'),
(10, 'UAE', '4C', 'PRIME', ARRAY['WRLC'], 'ADD', 5.00, 35, 0, TRUE, 'LME = (Total_Avg × 1.05) + 35', 'From Excel'),
(11, 'UAE', '4C', 'PRIME', ARRAY['WRHC'], 'ADD', 5.00, 35, 66, TRUE, 'LME = (Total_Avg × 1.05) + 101', '+66 freight'),
(1, 'UAE', '1', 'PRIME', ARRAY['HRP'], 'SUBTRACT', 5.00, 35, 0, FALSE, 'LME = Average × 0.95 + 35', 'Exception: UAE HRP');

-- LC Master
CREATE TABLE lc_master (
    lc_id SERIAL PRIMARY KEY,
    lc_number VARCHAR(50) UNIQUE NOT NULL,
    contract_number VARCHAR(50),
    supplier_name VARCHAR(200),
    importer_name VARCHAR(200),
    lc_date DATE NOT NULL,
    expiry_date DATE,
    last_ship_date DATE,
    lc_amount NUMERIC(15,2),
    currency VARCHAR(10),
    hoa VARCHAR(100),
    payment_terms VARCHAR(200),
    monitoring_expiry DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','SHIPPED','EXPIRED','CANCELLED')),
    status_changed_at TIMESTAMP,
    status_changed_by INTEGER REFERENCES users(user_id),
    status_notes TEXT,
    reopen_requested BOOLEAN DEFAULT FALSE,
    reopen_requested_by INTEGER REFERENCES users(user_id),
    reopen_requested_at TIMESTAMP,
    reopen_notes TEXT,
    created_by INTEGER REFERENCES users(user_id),
    assigned_to INTEGER REFERENCES users(user_id),
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_lc_status ON lc_master(status);
CREATE INDEX idx_lc_monitoring_expiry ON lc_master(monitoring_expiry);
CREATE INDEX idx_lc_date ON lc_master(lc_date);
CREATE INDEX idx_lc_assigned_to ON lc_master(assigned_to);

-- LC Products
CREATE TABLE lc_products (
    line_id SERIAL PRIMARY KEY,
    lc_id INTEGER NOT NULL REFERENCES lc_master(lc_id) ON DELETE CASCADE,
    product_code VARCHAR(20) NOT NULL,
    product_name VARCHAR(100),
    origin VARCHAR(50) NOT NULL,
    quality VARCHAR(20) NOT NULL CHECK (quality IN ('PRIME','SECONDARY','PRM','SEC')),
    cargo_nature VARCHAR(20),
    quantity NUMERIC(12,2) NOT NULL,
    unit VARCHAR(10) DEFAULT 'MT',
    lc_unit_price NUMERIC(10,2) NOT NULL,
    current_lme_price NUMERIC(10,2),
    hs_code VARCHAR(20),
    grade VARCHAR(50),
    size VARCHAR(50),
    last_lme_update TIMESTAMP,
    change_detected BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_product_lc ON lc_products(lc_id);
CREATE INDEX idx_product_code ON lc_products(product_code);
CREATE INDEX idx_product_origin ON lc_products(origin);
CREATE INDEX idx_change_detected ON lc_products(change_detected);

-- LME Bulletins
CREATE TABLE lme_bulletins (
    bulletin_id SERIAL PRIMARY KEY,
    bulletin_date DATE NOT NULL,
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    uploaded_by INTEGER REFERENCES users(user_id),
    processed BOOLEAN DEFAULT FALSE,
    processing_started TIMESTAMP,
    processing_completed TIMESTAMP,
    processing_errors TEXT,
    extracted_data JSONB,
    total_prices_extracted INTEGER,
    UNIQUE(bulletin_date, file_name)
);

CREATE INDEX idx_bulletin_date ON lme_bulletins(bulletin_date DESC);
CREATE INDEX idx_bulletin_processed ON lme_bulletins(processed);
CREATE INDEX idx_bulletin_uploaded_by ON lme_bulletins(uploaded_by);

-- LME Prices
CREATE TABLE lme_prices (
    price_id SERIAL PRIMARY KEY,
    bulletin_id INTEGER REFERENCES lme_bulletins(bulletin_id) ON DELETE CASCADE,
    fastmarket_symbol VARCHAR(50),
    product_code VARCHAR(20) NOT NULL,
    origin VARCHAR(50) NOT NULL,
    quality VARCHAR(20),
    low_price NUMERIC(10,2),
    high_price NUMERIC(10,2),
    calculated_lme NUMERIC(10,2),
    formula_used VARCHAR(20),
    calculation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    eur_rate NUMERIC(10,6),
    usd_rate NUMERIC(10,6),
    conversion_rate NUMERIC(10,6),
    notes TEXT,
    CHECK (low_price <= high_price)
);

CREATE INDEX idx_lme_bulletin ON lme_prices(bulletin_id);
CREATE INDEX idx_lme_product ON lme_prices(product_code, origin, quality);
CREATE INDEX idx_lme_date ON lme_prices(calculation_date DESC);

-- Price Alerts
CREATE TABLE price_alerts (
    alert_id SERIAL PRIMARY KEY,
    lc_id INTEGER NOT NULL REFERENCES lc_master(lc_id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES lc_products(line_id) ON DELETE CASCADE,
    alert_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    alert_type VARCHAR(20) NOT NULL CHECK (alert_type IN ('DECREASE','INCREASE')),
    priority VARCHAR(20) DEFAULT 'MEDIUM' CHECK (priority IN ('HIGH','MEDIUM','LOW')),
    old_lme_price NUMERIC(10,2),
    new_lme_price NUMERIC(10,2),
    lc_price NUMERIC(10,2),
    difference NUMERIC(10,2),
    difference_percent NUMERIC(5,2),
    quantity NUMERIC(12,2),
    whatsapp_sent BOOLEAN DEFAULT FALSE,
    whatsapp_sent_at TIMESTAMP,
    whatsapp_delivery_status VARCHAR(50),
    whatsapp_message_id VARCHAR(100),
    viewed BOOLEAN DEFAULT FALSE,
    viewed_at TIMESTAMP,
    viewed_by INTEGER REFERENCES users(user_id),
    action_taken VARCHAR(50),
    action_date TIMESTAMP,
    action_by INTEGER REFERENCES users(user_id),
    action_notes TEXT
);

CREATE INDEX idx_alert_lc ON price_alerts(lc_id);
CREATE INDEX idx_alert_type ON price_alerts(alert_type);
CREATE INDEX idx_alert_date ON price_alerts(alert_date DESC);
CREATE INDEX idx_alert_viewed ON price_alerts(viewed);
CREATE INDEX idx_whatsapp_sent ON price_alerts(whatsapp_sent);
CREATE INDEX idx_alert_action_by ON price_alerts(action_by);

-- WhatsApp Config
CREATE TABLE whatsapp_config (
    config_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    recipient_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    country_code VARCHAR(5) DEFAULT '+92',
    alert_type VARCHAR(50) DEFAULT 'ALL',
    min_amount_threshold NUMERIC(10,2),
    min_percent_threshold NUMERIC(5,2),
    active BOOLEAN DEFAULT TRUE,
    last_message_sent TIMESTAMP,
    total_messages_sent INTEGER DEFAULT 0,
    send_daily_summary BOOLEAN DEFAULT FALSE,
    send_weekly_report BOOLEAN DEFAULT FALSE,
    quiet_hours_start TIME,
    quiet_hours_end TIME,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_whatsapp_user ON whatsapp_config(user_id);

INSERT INTO whatsapp_config (user_id, recipient_name, phone_number, alert_type, active) VALUES
(1, 'System Admin', '+923001234567', 'ALL', TRUE),
(2, 'Manager - Ahsan', '+923009876543', 'ALL', TRUE);

-- Audit Log
CREATE TABLE audit_log (
    log_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    action VARCHAR(100) NOT NULL,
    entity_type VARCHAR(50),
    entity_id INTEGER,
    old_value JSONB,
    new_value JSONB,
    description TEXT,
    ip_address INET,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_date ON audit_log(created_at DESC);

COMMENT ON DATABASE lme_monitoring IS 'LME Monitoring System v2.0 - PostgreSQL 18 Compatible';
