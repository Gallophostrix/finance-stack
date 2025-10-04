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
  kind              TEXT   NOT NULL CHECK (kind IN ('in','out','fee','interest')),
  source_price      TEXT   NOT NULL,           -- 'coingecko_eod' | 'legacy-seed' | ...
  observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flows_eur_account_date ON derived.flows_eur (account_id, d);
CREATE INDEX IF NOT EXISTS idx_flows_eur_asset_date   ON derived.flows_eur (asset, d);

-- Performance results (e.g. MWR/TWR) per period
-- Note: one single simple table; we encode the "dimension" via 'level' and fill unused fields with ''/0.
CREATE TABLE IF NOT EXISTS derived.mwr (
  as_of       DATE NOT NULL,                                         -- end of period (month)
  level       TEXT NOT NULL CHECK (level IN ('asset','account','portfolio','category')),
  category    TEXT NOT NULL DEFAULT '',                               -- if level='category', else ''
  asset       TEXT NOT NULL DEFAULT '',                               -- if level='asset', else ''
  account_id  BIGINT NOT NULL DEFAULT 0,                              -- if level='account', else 0
  irr         NUMERIC(18,8),                                          -- ex: 0.01234567 = 1.234567%
  PRIMARY KEY (as_of, level, category, asset, account_id)
);

CREATE INDEX IF NOT EXISTS idx_mwr_by_level ON derived.mwr (level, as_of);
