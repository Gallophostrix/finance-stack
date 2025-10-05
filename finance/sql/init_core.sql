-- ============================================
-- Core schema (Layer A) : dimensions + native facts
-- ============================================

CREATE SCHEMA IF NOT EXISTS core;

-- ---------- Dimensions ----------

-- Assets (BTC, ADA, WORLD, FONDS_EURO, ...)
CREATE TABLE IF NOT EXISTS core.assets (
  asset_code    TEXT PRIMARY KEY,                                -- ex: 'BTC', 'ADA', 'WORLD', 'FONDS_EURO'
  class         TEXT NOT NULL CHECK (class IN ('crypto','actions','epargne')),
  decimals      SMALLINT NOT NULL DEFAULT 0,                     -- 8 for BTC, 6 for ADA, 2 for EUR, etc.
  coingecko_id  TEXT,                                            -- for the layer B
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Providers/sources (bitcoin, cardano, pea, bank, av, ...)
CREATE TABLE IF NOT EXISTS core.providers (
  provider_id    BIGSERIAL PRIMARY KEY,
  provider_type  TEXT NOT NULL CHECK (provider_type IN ('blockchain','bank','broker','cex','other')),
  provider_name  TEXT NOT NULL,                                  -- ex: 'bitcoin', 'cardano', 'amundi-pea', 'bforbank'
  UNIQUE (provider_type, provider_name)
);

-- Accounts/containers (BTC address, stake key, IBAN, PEA/AV accounts, etc.)
CREATE TABLE IF NOT EXISTS core.accounts (
  account_id           BIGSERIAL PRIMARY KEY,
  provider_id          BIGINT NOT NULL REFERENCES core.providers(provider_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  account_type         TEXT NOT NULL CHECK (account_type IN ('address','stake_key','iban','broker_account','other')),
  external_identifier  TEXT NOT NULL,                            -- adresse/stake key/IBAN/id broker...
  label                TEXT,                                     -- "Ledger-1", "PEA Amundi", "Livret A ..."
  "group"              TEXT NOT NULL CHECK ("group" IN ('wallet','CEX','PEA','AV','Bank')),
  is_active            BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (provider_id, account_type, external_identifier)
);

-- ---------- Facts (native assets) ----------

-- Values provided in native units (ex: BTC, ADA, etc.), during a "cut" date (usually month-start)
CREATE TABLE IF NOT EXISTS core.balances_native (
  d              DATE   NOT NULL,                                -- cut date (UTC)
  account_id     BIGINT NOT NULL REFERENCES core.accounts(account_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  asset          TEXT   NOT NULL REFERENCES core.assets(asset_code) ON UPDATE CASCADE ON DELETE RESTRICT,
  amount_native  NUMERIC(38,18) NOT NULL CHECK (amount_native >= 0),
  observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (d, account_id, asset)
);

CREATE INDEX IF NOT EXISTS idx_balances_native_account_date ON core.balances_native (account_id, d);
CREATE INDEX IF NOT EXISTS idx_balances_native_asset_date   ON core.balances_native (asset, d);

-- Daily flows, in NATIVE units (per account/address)
CREATE TABLE IF NOT EXISTS core.flows_native (
  flow_uid       TEXT PRIMARY KEY,                               -- global idempotence (ex: 'bitcoin:<tx_hash>:<account_id>:<io_index>:<kind>')
  d              DATE   NOT NULL,                                -- tx date (UTC)
  account_id     BIGINT NOT NULL REFERENCES core.accounts(account_id) ON UPDATE CASCADE ON DELETE RESTRICT,
  asset          TEXT   NOT NULL REFERENCES core.assets(asset_code) ON UPDATE CASCADE ON DELETE RESTRICT,
  amount_native  NUMERIC(38,18) NOT NULL CHECK (amount_native > 0),
  kind           TEXT   NOT NULL CHECK (kind IN ('in','out','fee','interest')),
  origin_ref     TEXT,                                           -- tx_hash / booking_id...
);

CREATE INDEX IF NOT EXISTS idx_flows_native_account_date ON core.flows_native (account_id, d);
CREATE INDEX IF NOT EXISTS idx_flows_native_asset_date   ON core.flows_native (asset, d);
CREATE INDEX IF NOT EXISTS idx_flows_native_origin_ref   ON core.flows_native (origin_ref);
