-- ============================================
-- Market schema (Layer B) : API or manual seed of prices
-- ============================================

CREATE SCHEMA IF NOT EXISTS market;

-- Price in EUR per asset and per date (UTC), for the assets held
CREATE TABLE IF NOT EXISTS market.prices_eur (
  asset        TEXT NOT NULL REFERENCES core.assets(asset_code) ON UPDATE CASCADE ON DELETE RESTRICT,
  d            DATE NOT NULL,                                      -- Date (UTC, typically EOD)
  source       TEXT NOT NULL DEFAULT 'coingecko_eod',              -- Pricing source identifier
  price_eur    NUMERIC(20,8) NOT NULL CHECK (price_eur >= 0),
  observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (asset, d, source)
);

CREATE INDEX IF NOT EXISTS idx_prices_asset_date ON market.prices_eur (asset, d);
