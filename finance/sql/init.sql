-- === Référentiel des actifs (catégories / sous-catégories / actifs) ===
-- Cela verrouille les libellés et évite les typos au moment des saisies.
CREATE TABLE IF NOT EXISTS assets_catalog (
  category     TEXT NOT NULL CHECK (category IN ('liquidites','actions','tokens')),
  subcategory  TEXT NOT NULL,     -- liquidites: Compte courant, Espèces PEA, Livrets, Fonds euro
                                  -- actions: PEA, AV
                                  -- tokens: Wallet, CEX
  asset        TEXT NOT NULL,     -- ex: World, Emerging, Nasdaq, BTC, ADA, METERA, ...
  PRIMARY KEY (category, subcategory, asset)
);

-- Préremplir avec TON modèle actuel
INSERT INTO assets_catalog (category, subcategory, asset) VALUES
-- Liquidités
('liquidites','Compte courant','Compte courant'),
('liquidites','Espèces PEA','Espèces PEA'),
('liquidites','Livrets','Livrets'),
('liquidites','Fonds euro','Fonds euro'),
-- Actions (3 actifs, 2 sous-catégories possibles)
('actions','PEA','World'),
('actions','PEA','Emerging'),
('actions','PEA','Nasdaq'),
('actions','AV','World'),
('actions','AV','Emerging'),
('actions','AV','Nasdaq'),
-- Tokens (6 actifs, 2 sous-catégories)
('tokens','Wallet','BTC'),
('tokens','Wallet','ADA'),
('tokens','Wallet','METERA'),
('tokens','Wallet','INDY'),
('tokens','Wallet','iUSD'),
('tokens','Wallet','NIGHT'),
('tokens','CEX','BTC'),
('tokens','CEX','ADA'),
('tokens','CEX','METERA'),
('tokens','CEX','INDY'),
('tokens','CEX','iUSD'),
('tokens','CEX','NIGHT')
ON CONFLICT DO NOTHING;

-- === Table de saisie (mensuelle/ponctuelle), valeurs déjà en EUR ===
CREATE TABLE IF NOT EXISTS valuations (
  d           DATE NOT NULL,      -- date du relevé
  category    TEXT NOT NULL CHECK (category IN ('liquidites','actions','tokens')),
  subcategory TEXT NOT NULL,      -- voir assets_catalog
  asset       TEXT NOT NULL,      -- voir assets_catalog
  value_eur   NUMERIC(20,2) NOT NULL,
  PRIMARY KEY (d, category, subcategory, asset),
  FOREIGN KEY (category, subcategory, asset)
    REFERENCES assets_catalog (category, subcategory, asset)
    ON UPDATE CASCADE ON DELETE RESTRICT
);

-- Mouvements mensuels (apports/retraits/intérêts/frais)
CREATE TABLE IF NOT EXISTS flows (
  id SERIAL PRIMARY KEY,
  d           DATE NOT NULL,     -- date du mois (prends le dernier jour du mois par habitude)
  category    TEXT NOT NULL CHECK (category IN ('liquidites','actions','tokens')),
  subcategory TEXT NOT NULL,
  asset       TEXT NOT NULL,
  amount_eur  NUMERIC(20,2) NOT NULL,    -- +in/+interest ; -out/-fee
  kind        TEXT NOT NULL CHECK (kind IN ('in','out','interest','fee')),
  CONSTRAINT fk_flow_catalog
    FOREIGN KEY (category, subcategory, asset)
    REFERENCES assets_catalog (category, subcategory, asset)
      ON UPDATE CASCADE ON DELETE RESTRICT
);

-- Index utiles (mois / clé logique)
CREATE INDEX IF NOT EXISTS flows_by_month     ON flows (date_trunc('month', d));
CREATE INDEX IF NOT EXISTS flows_by_dimension ON flows (category, subcategory, asset, d);


-- Cibles globales (par catégorie)
CREATE TABLE targets_global (
  category TEXT PRIMARY KEY,
  target_pct NUMERIC(6,3) NOT NULL
);

-- Cibles internes (par actif, normalisées dans chaque catégorie)
CREATE TABLE targets_internal (
  category    TEXT NOT NULL,
  subcategory TEXT NOT NULL,
  asset       TEXT NOT NULL,
  target_pct  NUMERIC(6,3) NOT NULL,
  PRIMARY KEY (category, subcategory, asset)
);

-- MWR Table
CREATE TABLE IF NOT EXISTS mwr_results (
  as_of        DATE NOT NULL,                 -- period end (month)
  level        TEXT NOT NULL CHECK (level IN ('asset','category')),
  category     TEXT NOT NULL DEFAULT '',      -- filed if level='category' (ex: actions)
  asset        TEXT NOT NULL DEFAULT '',      -- filed if level='asset' (ex: BTC)
  irr          numeric(12,8),                 -- MWR in decimal nb (0.0123 = 1.23%)
  PRIMARY KEY (as_of, level, category, asset)
);
CREATE INDEX IF NOT EXISTS mwr_results_by_level ON mwr_results (level, as_of);


-- === Vues pratiques pour Grafana ===

-- Somme par date et catégorie (utile pour stacked area par catégories)
CREATE OR REPLACE VIEW v_sum_by_category AS
SELECT d, category, SUM(value_eur) AS value_eur
FROM valuations
GROUP BY d, category;

-- Portfolio global par date
CREATE OR REPLACE VIEW v_portfolio_total AS
SELECT d, SUM(value_eur) AS total_eur
FROM valuations
GROUP BY d;

-- Dernière répartition par catégorie
CREATE OR REPLACE VIEW v_latest_by_category AS
WITH last AS (SELECT MAX(d) md FROM valuations)
SELECT category, SUM(value_eur) AS value_eur
FROM valuations
WHERE d = (SELECT md FROM last)
GROUP BY category;

-- Dernière répartition détaillée (par sous-catégorie et actif)
CREATE OR REPLACE VIEW v_latest_detail AS
WITH last AS (SELECT MAX(d) md FROM valuations)
SELECT category, subcategory, asset, value_eur
FROM valuations
WHERE d = (SELECT md FROM last)
ORDER BY category, subcategory, value_eur DESC;
