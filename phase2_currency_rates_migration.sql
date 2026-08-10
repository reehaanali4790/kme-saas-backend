-- Phase 2: Currency Rates Table
-- Add this to your database

CREATE TABLE IF NOT EXISTS currency_rates (
    rate_id SERIAL PRIMARY KEY,
    rate_date DATE UNIQUE NOT NULL,
    usd_rate NUMERIC(10,4) NOT NULL CHECK (usd_rate > 0),
    eur_rate NUMERIC(10,4) NOT NULL CHECK (eur_rate > 0),
    source VARCHAR(50) DEFAULT 'Manual',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES users(user_id),
    updated_at TIMESTAMP,
    updated_by INTEGER REFERENCES users(user_id)
);

-- Create index for faster date lookups
CREATE INDEX idx_currency_rates_date ON currency_rates(rate_date DESC);

-- Insert sample rates for testing
INSERT INTO currency_rates (rate_date, usd_rate, eur_rate, source, notes) 
VALUES 
    ('2026-01-28', 278.50, 301.20, 'Manual', 'Sample rate for testing'),
    ('2026-02-05', 278.75, 301.50, 'Manual', 'Sample rate for testing')
ON CONFLICT (rate_date) DO NOTHING;

-- Add rate_id to lme_bulletins for tracking
ALTER TABLE lme_bulletins 
ADD COLUMN IF NOT EXISTS rate_id INTEGER REFERENCES currency_rates(rate_id);

COMMENT ON TABLE currency_rates IS 'Stores USD and EUR exchange rates for LME calculations';
COMMENT ON COLUMN currency_rates.rate_date IS 'Date for which these rates are applicable';
COMMENT ON COLUMN currency_rates.usd_rate IS 'USD to PKR exchange rate';
COMMENT ON COLUMN currency_rates.eur_rate IS 'EUR to PKR exchange rate';
COMMENT ON COLUMN currency_rates.source IS 'Source of rate (Manual, NBP, etc)';
