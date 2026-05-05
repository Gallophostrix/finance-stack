# 📘 Layer A — Core Schema (`core`)

**Purpose:**  
Foundational layer containing **dimension tables** (assets, providers, accounts) and **native fact tables** (balances and flows).  
Acts as the single source of truth for all raw portfolio data before conversion or aggregation.

---

## 🧭 PSQL Basics

| Action                | Command                                          |
| --------------------- | ------------------------------------------------ |
| Connect to DB         | `psql -h <HOST> -U <USER> -d <DB>`               |
| List schemas & tables | `\dn+ core` / `\dt+ core.*`                      |
| List indexes          | `\di+ core.*`                                    |
| Describe a table      | `\d+ core.<table_name>`                          |
| Transaction           | `BEGIN; -- your SQL -- COMMIT;` (or `ROLLBACK;`) |

---

## 🧱 Tables Overview

### **1️⃣ core.assets**

**Role:** Catalog of all assets/tickers (e.g., BTC, ADA, EUR)

**Primary key:** `asset_code`  
**Checks:** `class IN ('crypto','actions','epargne')`

| Column       | Type            | Description                               |
| ------------ | --------------- | ----------------------------------------- |
| asset_code   | text            | Unique symbol (`'BTC'`, `'ADA'`, `'EUR'`) |
| class        | text            | `'crypto'`, `'actions'`, or `'epargne'`   |
| decimals     | smallint        | Native precision (BTC=8, ADA=6, EUR=2)    |
| coingecko_id | text (nullable) | External mapping ID                       |
| is_active    | boolean         | Active flag (default TRUE)                |
| created_at   | timestamptz     | Creation timestamp                        |

**View**

```sql
\d+ core.assets;
SELECT * FROM core.assets ORDER BY asset_code;
```

**Modify**

```sql
-- Upsert
INSERT INTO core.assets(asset_code,class,decimals,coingecko_id,is_active)
VALUES ('BTC','crypto',8,'bitcoin',TRUE)
ON CONFLICT (asset_code) DO UPDATE
  SET class=EXCLUDED.class,
      decimals=EXCLUDED.decimals,
      coingecko_id=EXCLUDED.coingecko_id,
      is_active=EXCLUDED.is_active;

-- Deactivate
UPDATE core.assets SET is_active = FALSE WHERE asset_code='XYZ';
```

---

### **2️⃣ core.providers**

**Role:** List of all data sources (blockchains, banks, brokers, etc.)

**Primary key:** `provider_id (bigserial)`
**Unique constraint:** `(provider_type, provider_name)`

| Column        | Type      | Description                                              |
| ------------- | --------- | -------------------------------------------------------- |
| provider_id   | bigserial | PK                                                       |
| provider_type | text      | `'blockchain'`, `'bank'`, `'broker'`, `'cex'`, `'other'` |
| provider_name | text      | e.g. `'bitcoin'`, `'cardano'`, `'amundi-pea'`            |

**View**

```sql
\d+ core.providers;
SELECT * FROM core.providers ORDER BY provider_type, provider_name;
```

**Modify**

```sql
INSERT INTO core.providers(provider_type, provider_name)
VALUES ('blockchain','bitcoin')
ON CONFLICT (provider_type, provider_name) DO NOTHING
RETURNING provider_id;

UPDATE core.providers
SET provider_name = 'amundi-pea'
WHERE provider_type='broker' AND provider_name='amundi';
```

---

### **3️⃣ core.accounts**

**Role:** Represents all account containers (addresses, IBANs, broker accounts, etc.)

**Primary key:** `account_id (bigserial)`
**Unique constraint:** `(provider_id, account_type, external_identifier)`
**Foreign key:** `provider_id → core.providers(provider_id)`

| Column              | Type      | Description                                                         |
| ------------------- | --------- | ------------------------------------------------------------------- |
| account_id          | bigserial | PK                                                                  |
| provider_id         | bigint    | FK to `core.providers`                                              |
| account_type        | text      | `'address'`, `'stake_key'`, `'iban'`, `'broker_account'`, `'other'` |
| external_identifier | text      | Unique address / IBAN / identifier                                  |
| label               | text      | Human-readable alias (`"Ledger-1"`, `"PEA Amundi"`)                 |
| group               | text      | `'wallet'`, `'CEX'`, `'PEA'`, `'AV'`, `'Bank'`                      |
| is_active           | boolean   | Default TRUE                                                        |

**View**

```sql
\d+ core.accounts;
SELECT * FROM core.accounts WHERE is_active;
```

**Modify**

```sql
INSERT INTO core.accounts(provider_id,account_type,external_identifier,label,"group",is_active)
VALUES (
  (SELECT provider_id FROM core.providers WHERE provider_type='blockchain' AND provider_name='bitcoin'),
  'address','bc1q...','Ledger-1','wallet',TRUE
)
ON CONFLICT (provider_id,account_type,external_identifier) DO UPDATE
  SET label=EXCLUDED.label, "group"=EXCLUDED."group", is_active=EXCLUDED.is_active;

UPDATE core.accounts SET is_active=FALSE WHERE account_id=123;
```

---

### **4️⃣ core.balances_native**

**Role:** Periodic snapshots of balances per (date, account, asset) in native units.

**Primary key:** `(d, account_id, asset)`
**Foreign keys:**

- `account_id → core.accounts(account_id)`
- `asset → core.assets(asset_code)`

| Column        | Type           | Description                 |
| ------------- | -------------- | --------------------------- |
| d             | date           | Cut date (UTC, month-start) |
| account_id    | bigint         | FK to accounts              |
| asset         | text           | FK to assets                |
| amount_native | numeric(38,18) | ≥ 0, native units           |
| observed_at   | timestamptz    | When the value was recorded |

**View**

```sql
\d+ core.balances_native;
SELECT * FROM core.balances_native ORDER BY d;
```

**Modify**

```sql
INSERT INTO core.balances_native(d,account_id,asset,amount_native,observed_at)
VALUES ('2025-10-01', 123, 'BTC', 0.42100000, NOW())
ON CONFLICT (d,account_id,asset) DO UPDATE
  SET amount_native = EXCLUDED.amount_native,
      observed_at   = EXCLUDED.observed_at;
```

---

### **5️⃣ core.flows_native**

**Role:** Daily movements (in/out/fee/interest) in native units, one row per transaction leg.

**Primary key:** `flow_uid`
**Foreign keys:**

- `account_id → core.accounts(account_id)`
- `asset → core.assets(asset_code)`

| Column        | Type           | Description                                                    |
| ------------- | -------------- | -------------------------------------------------------------- |
| flow_uid      | text           | Unique identifier (`bitcoin:<tx>:<acct_id>:<io_index>:<kind>`) |
| d             | date           | UTC transaction date                                           |
| account_id    | bigint         | FK to accounts                                                 |
| asset         | text           | FK to assets                                                   |
| amount_native | numeric(38,18) | Positive amount                                                |
| kind          | text           | `'in'`, `'out'`, `'fee'`, `'interest'`                         |
| origin_ref    | text           | Transaction hash / booking ID                                  |

**View**

```sql
\d+ core.flows_native;
SELECT * FROM core.flows_native ORDER BY d DESC LIMIT 20;
```

**Modify**

```sql
INSERT INTO core.flows_native(flow_uid,d,account_id,asset,amount_native,kind,origin_ref)
VALUES ('bitcoin:<tx_hash>:123:0:in','2025-10-05',123,'BTC',0.005,'in','<tx_hash>')
ON CONFLICT (flow_uid) DO NOTHING;
```

---

## 🧪 Sanity Checks

| Check                             | Query                                                                                                                         |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Orphaned accounts in balances     | `sql SELECT COUNT(*) FROM core.balances_native b LEFT JOIN core.accounts a USING(account_id) WHERE a.account_id IS NULL;`     |
| Orphaned accounts in flows        | `sql SELECT COUNT(*) FROM core.flows_native f LEFT JOIN core.accounts a USING(account_id) WHERE a.account_id IS NULL;`        |
| Orphaned assets in balances       | `sql SELECT COUNT(*) FROM core.balances_native b LEFT JOIN core.assets s ON s.asset_code=b.asset WHERE s.asset_code IS NULL;` |
| Orphaned assets in flows          | `sql SELECT COUNT(*) FROM core.flows_native f LEFT JOIN core.assets s ON s.asset_code=f.asset WHERE s.asset_code IS NULL;`    |
| Duplicate primary keys (balances) | `sql SELECT d,account_id,asset,COUNT(*) FROM core.balances_native GROUP BY 1,2,3 HAVING COUNT(*)>1;`                          |
| Duplicate primary keys (flows)    | `sql SELECT flow_uid,COUNT(*) FROM core.flows_native GROUP BY 1 HAVING COUNT(*)>1;`                                           |

---

## ⚙️ Maintenance Commands

```sql
ANALYZE core.assets;
ANALYZE core.providers;
ANALYZE core.accounts;
ANALYZE core.balances_native;
ANALYZE core.flows_native;

-- After large imports
VACUUM (ANALYZE);
```

---

**Summary:**

- Populate dimensions first: `assets → providers → accounts`
- Then feed fact tables: `balances_native` and `flows_native`
- Run sanity checks regularly
- Use `ANALYZE` after big inserts for query planner accuracy
