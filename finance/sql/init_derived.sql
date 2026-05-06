-- ============================================
-- Derived schema (Layer C) : EUR projections + performance
-- ============================================

CREATE SCHEMA IF NOT EXISTS derived;

-- EUR projected balances (through API or manual seed)
CREATE TABLE IF NOT EXISTS derived.balances_eur (
  d             DATE   NOT NULL,
  account_id    BIGINT NOT NULL REFERENCES core.accounts(account_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  asset         TEXT   NOT NULL REFERENCES core.assets(asset_code)   ON UPDATE CASCADE ON DELETE RESTRICT,
  value_eur     NUMERIC(20,2) NOT NULL,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (d, account_id, asset)
);

CREATE INDEX IF NOT EXISTS idx_balances_eur_account_date ON derived.balances_eur (account_id, d);
CREATE INDEX IF NOT EXISTS idx_balances_eur_asset_date   ON derived.balances_eur (asset, d);

-- EUR flow projections (via API or manual seed of prices at 'd' day)
CREATE TABLE IF NOT EXISTS derived.flows_eur (
  native_flow_uid   TEXT PRIMARY KEY REFERENCES core.flows_native(flow_uid) ON UPDATE CASCADE ON DELETE CASCADE,
  d                 DATE   NOT NULL,
  account_id        BIGINT NOT NULL REFERENCES core.accounts(account_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  asset             TEXT   NOT NULL REFERENCES core.assets(asset_code)   ON UPDATE CASCADE ON DELETE RESTRICT,
  amount_eur        NUMERIC(20,2) NOT NULL,
  kind              TEXT   NOT NULL CHECK (kind IN ('in','out','interest')),
  source_price      TEXT   NOT NULL,           -- 'coingecko_eod' | 'legacy-seed' | ...
  observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flows_eur_account_date ON derived.flows_eur (account_id, d);
CREATE INDEX IF NOT EXISTS idx_flows_eur_asset_date   ON derived.flows_eur (asset, d);

-- Performance results (e.g. MWR/TWR) per period
-- Note: one single simple table; we encode the "dimension" via 'level' and fill unused fields with ''/0.
CREATE TABLE IF NOT EXISTS derived.returns_itd (
  as_of       DATE NOT NULL,      -- snapshot date (e.g. start of current month)
  metric      TEXT NOT NULL
              CHECK (metric IN ('twr','mwr')),
  level       TEXT NOT NULL
              CHECK (level IN ('asset','category')),

  category    TEXT NOT NULL,       -- always set
  asset       TEXT NOT NULL DEFAULT '',  -- only if level='asset'

  value       NUMERIC(18,8),       -- ratio (0.01 = +1%)
  computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Semantic consistency
  CHECK (
    (level = 'category' AND asset = '') OR
    (level = 'asset'    AND asset <> '')
  ),

  -- Business rules (documented, enforced in code)
  -- level='asset'    → metric='twr'
  -- level='category' → metric IN ('twr','mwr')

  PRIMARY KEY (as_of, metric, level, category, asset)
);

CREATE INDEX IF NOT EXISTS idx_returns_itd_by_metric_level
  ON derived.returns_itd (metric, level, as_of);

CREATE INDEX IF NOT EXISTS idx_returns_itd_category
  ON derived.returns_itd (category, as_of);

CREATE INDEX IF NOT EXISTS idx_returns_itd_asset
  ON derived.returns_itd (asset, as_of)
  WHERE level = 'asset';
