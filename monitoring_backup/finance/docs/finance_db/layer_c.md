# 💹 Layer C — Derived Schema (`derived`)

**Purpose:**  
EUR **projections** and **performance** computed from Layer A (**core**) and Layer B (**market**).

- `balances_eur`: monthly EUR valuation snapshots per `(d, account_id, asset)`
- `flows_eur` : EUR-valued flows (1 row per **native** flow)
- `mwr` : Money-Weighted Return (IRR) results by period and aggregation level

---

## 🧭 PSQL Basics

| Action             | Command                             |
| ------------------ | ----------------------------------- |
| Connect            | `psql -h <HOST> -U <USER> -d <DB>`  |
| List schema/tables | `\dn+ derived` / `\dt+ derived.*`   |
| List indexes       | `\di+ derived.*`                    |
| Describe table     | `\d+ derived.<table_name>`          |
| Transaction        | `BEGIN; … COMMIT;` (or `ROLLBACK;`) |

---

## 🧱 Tables Overview

---

### 1) `derived.balances_eur`

**Role:** EUR snapshots per `(month, account, asset)`.

**PK:** `(d, account_id, asset)`  
**FKs:** `account_id → core.accounts`, `asset → core.assets`

| Column      | Type          | Notes                                    |
| ----------- | ------------- | ---------------------------------------- |
| d           | date          | Month cut (UTC) — typically `YYYY-MM-01` |
| account_id  | bigint        | FK to accounts                           |
| asset       | text          | FK to assets                             |
| value_eur   | numeric(20,2) | EUR value (rounded to cents)             |
| observed_at | timestamptz   | Ingestion timestamp                      |

**View**

```sql
\d+ derived.balances_eur;
SELECT * FROM derived.balances_eur WHERE d >= date_trunc('month', CURRENT_DATE) - INTERVAL '6 months'
ORDER BY d DESC, account_id, asset;
```

**Insert / Upsert (manual)**

```sql
INSERT INTO derived.balances_eur(d,account_id,asset,value_eur,observed_at)
VALUES ('2025-10-01',123,'BTC', 28000.00, NOW())
ON CONFLICT (d,account_id,asset) DO UPDATE
  SET value_eur = EXCLUDED.value_eur,
      observed_at = EXCLUDED.observed_at;
```

**Populate from `core.balances_native × market.prices_eur`**

```sql
INSERT INTO derived.balances_eur(d,account_id,asset,value_eur,observed_at)
SELECT b.d, b.account_id, b.asset,
       ROUND(b.amount_native * p.price_eur, 2) AS value_eur,
       NOW()
FROM core.balances_native b
JOIN market.prices_eur p
  ON p.asset = b.asset AND p.d = b.d
WHERE p.source = 'coingecko_eod'
ON CONFLICT (d,account_id,asset) DO UPDATE
  SET value_eur = EXCLUDED.value_eur,
      observed_at = EXCLUDED.observed_at;
```

---

### 2) `derived.flows_eur`

**Role:** EUR valuation of **each native flow** on its transaction date `d`.

**PK:** `native_flow_uid` (FK to `core.flows_native(flow_uid)`)
**FKs:** `account_id → core.accounts`, `asset → core.assets`

| Column          | Type          | Notes                                            |
| --------------- | ------------- | ------------------------------------------------ | ------------- | ----- | ---------- |
| native_flow_uid | text          | Same ID as `core.flows_native.flow_uid` (1-to-1) |
| d               | date          | UTC flow date (copy from native)                 |
| account_id      | bigint        | FK to accounts                                   |
| asset           | text          | FK to assets                                     |
| amount_eur      | numeric(20,2) | Flow amount valued in EUR (positive)             |
| kind            | text          | `in`                                             | `out`         | `fee` | `interest` |
| source_price    | text          | e.g. `coingecko_eod`                             | `legacy-seed` |
| observed_at     | timestamptz   | Ingestion timestamp                              |

**View**

```sql
\d+ derived.flows_eur;
SELECT * FROM derived.flows_eur ORDER BY d DESC, native_flow_uid LIMIT 50;
```

**Populate from `core.flows_native × market.prices_eur`**

```sql
INSERT INTO derived.flows_eur(native_flow_uid,d,account_id,asset,amount_eur,kind,source_price,observed_at)
SELECT
  f.flow_uid,
  f.d,
  f.account_id,
  f.asset,
  ROUND(f.amount_native * p.price_eur, 2) AS amount_eur,
  f.kind,
  'coingecko_eod' AS source_price,
  NOW()
FROM core.flows_native f
JOIN market.prices_eur p
  ON p.asset = f.asset AND p.d = f.d
WHERE p.source = 'coingecko_eod'
ON CONFLICT (native_flow_uid) DO UPDATE
  SET amount_eur   = EXCLUDED.amount_eur,
      source_price = EXCLUDED.source_price,
      observed_at  = EXCLUDED.observed_at;
```

Direction convention: `amount_native` is **always positive**; the **sign** of the cash flow is given by `kind`.
For reporting, compute signed EUR when needed:

> ```sql
> SELECT CASE kind
>          WHEN 'in' THEN amount_eur
>          WHEN 'interest' THEN amount_eur
>          WHEN 'out' THEN -amount_eur
>          WHEN 'fee' THEN -amount_eur
>        END AS signed_amount_eur
> FROM derived.flows_eur;
> ```

---

### 3) `derived.mwr`

**Role:** Money-Weighted Return (IRR) results by month (`as_of`) and **aggregation level**.

**PK:** `(as_of, level, category, asset, account_id)`
**Levels:** `asset` | `account` | `portfolio` | `category`

| Column     | Type          | Notes                                   |
| ---------- | ------------- | --------------------------------------- |
| as_of      | date          | Period end (month), e.g. `YYYY-MM-01`   |
| level      | text          | one of the levels above                 |
| category   | text          | used if `level='category'`, else `''`   |
| asset      | text          | used if `level='asset'`, else `''`      |
| account_id | bigint        | used if `level='account'`, else `0`     |
| irr        | numeric(18,8) | IRR as a decimal (`0.0123` = **1.23%**) |

**View**

```sql
\d+ derived.mwr;
SELECT * FROM derived.mwr WHERE level='portfolio' ORDER BY as_of DESC;
```

**Insert / Upsert**

```sql
INSERT INTO derived.mwr(as_of,level,category,asset,account_id,irr)
VALUES ('2025-10-01','portfolio','', '', 0, 0.01876321)
ON CONFLICT (as_of,level,category,asset,account_id) DO UPDATE
  SET irr = EXCLUDED.irr;
```

**Notes on computing IRR (MWR):**

- Build a **cash-flow series** over the period with **signed** EUR amounts from `derived.flows_eur` plus terminal value from `derived.balances_eur`.
- Solve for `irr` in: `NPV(irr) = 0` (Newton / secant method or a small SQL/PLpgSQL helper).
- Keep 6–8 decimals for IRR storage (`numeric(18,8)`), display as percentage with formatting.

---

## 🔍 Useful Queries

**Monthly EUR portfolio snapshot (sum of all accounts/assets)**

```sql
SELECT d, SUM(value_eur) AS portfolio_value_eur
FROM derived.balances_eur
GROUP BY d
ORDER BY d;
```

**Monthly EUR by asset class (joining `core.assets.class`)**

```sql
SELECT b.d, a.class, SUM(b.value_eur) AS value_eur
FROM derived.balances_eur b
JOIN core.assets a ON a.asset_code = b.asset
GROUP BY b.d, a.class
ORDER BY b.d, a.class;
```

**Flows EUR signed by day (last 30 days)**

```sql
SELECT d,
       SUM(CASE kind WHEN 'in' THEN amount_eur WHEN 'interest' THEN amount_eur
                     WHEN 'out' THEN -amount_eur WHEN 'fee' THEN -amount_eur END) AS net_flow_eur
FROM derived.flows_eur
WHERE d >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY d
ORDER BY d DESC;
```

---

## 🧪 Sanity Checks

```sql
-- 1) Missing prices when projecting (should be 0 if Layer B is complete)
SELECT COUNT(*) AS missing_prices
FROM core.balances_native b
LEFT JOIN market.prices_eur p ON p.asset=b.asset AND p.d=b.d AND p.source='coingecko_eod'
WHERE p.asset IS NULL;

-- 2) Consistency: balances_eur must have 1 row per (d, account_id, asset)
SELECT d, account_id, asset, COUNT(*)
FROM derived.balances_eur
GROUP BY 1,2,3 HAVING COUNT(*)>1;

-- 3) Flows: 1-to-1 with native flows if fully projected
SELECT
  (SELECT COUNT(*) FROM core.flows_native)  AS native_count,
  (SELECT COUNT(*) FROM derived.flows_eur)  AS eur_count;
```

---

## ⚙️ Maintenance

```sql
ANALYZE derived.balances_eur;
ANALYZE derived.flows_eur;
ANALYZE derived.mwr;

-- After large backfills
VACUUM (ANALYZE) derived.balances_eur;
VACUUM (ANALYZE) derived.flows_eur;
```

---

## ✅ Best Practices

- Keep **prices** and **balances** aligned on the same UTC date key `d`.
- Use **`ON CONFLICT … DO UPDATE`** for idempotent backfills.
- Store `value_eur` / `amount_eur` at **2 decimals**; compute with higher precision then round.
- Always record `source_price` in `flows_eur` for auditability.
- Prefer **versioned** monthly runs (immutable inputs) before computing MWR to ensure reproducibility.

**Summary:** Layer C transforms native quantities (Layer A) using prices (Layer B) into **EUR valuations** and **performance metrics**, ready for dashboards and reports.
