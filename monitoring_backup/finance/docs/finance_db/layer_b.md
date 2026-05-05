# 💱 Layer B — Market Schema (`market`)

**Purpose:**  
Holds historical **price data (in EUR)** per asset and per date.  
This layer enriches the _core_ balances with market valuation capability — transforming native amounts into EUR equivalents for performance and reporting layers.

---

## 🧭 PSQL Basics

| Action                | Command                                          |
| --------------------- | ------------------------------------------------ |
| Connect to DB         | `psql -h <HOST> -U <USER> -d <DB>`               |
| List schemas & tables | `\dn+ market` / `\dt+ market.*`                  |
| List indexes          | `\di+ market.*`                                  |
| Describe table        | `\d+ market.prices_eur`                          |
| Transaction           | `BEGIN; -- your SQL -- COMMIT;` (or `ROLLBACK;`) |

---

## 📈 Table: `market.prices_eur`

**Role:**  
Stores asset prices in EUR, one record per `(asset, date, source)`.

**Primary key:** `(asset, d, source)`  
**Foreign key:** `asset → core.assets(asset_code)`  
**Checks:** `price_eur >= 0`

| Column      | Type          | Description                                                        |
| ----------- | ------------- | ------------------------------------------------------------------ |
| asset       | text          | Asset code (`'BTC'`, `'ADA'`, `'EUR'`, etc.) — FK to `core.assets` |
| d           | date          | Date (UTC) — usually End of Day (EOD)                              |
| source      | text          | Price feed identifier (default `'coingecko_eod'`)                  |
| price_eur   | numeric(20,8) | Price in EUR for 1 unit of the asset                               |
| observed_at | timestamptz   | Ingestion timestamp (default `NOW()`)                              |

---

## 🔍 Viewing Data

| Goal                                 | SQL                                                                                                        |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| Describe table                       | `\d+ market.prices_eur`                                                                                    |
| List all prices (last 30 days)       | `sql SELECT * FROM market.prices_eur WHERE d >= CURRENT_DATE - INTERVAL '30 days' ORDER BY d DESC, asset;` |
| Show prices for one asset            | `sql SELECT d, price_eur FROM market.prices_eur WHERE asset='BTC' ORDER BY d DESC;`                        |
| Get latest available price per asset | `sql SELECT DISTINCT ON (asset) asset, d, price_eur FROM market.prices_eur ORDER BY asset, d DESC;`        |

---

## ✏️ Modifying Data

### Insert / Upsert Example

```sql
INSERT INTO market.prices_eur(asset,d,source,price_eur,observed_at)
VALUES
  ('BTC','2025-10-11','coingecko_eod',67000,NOW()),
  ('ADA','2025-10-11','coingecko_eod',0.45,NOW())
ON CONFLICT (asset,d,source) DO UPDATE
  SET price_eur = EXCLUDED.price_eur,
      observed_at = EXCLUDED.observed_at;
```

### Update Example

```sql
UPDATE market.prices_eur
SET price_eur = 0.47
WHERE asset='ADA' AND d='2025-10-11' AND source='coingecko_eod';
```

### Delete Example

```sql
DELETE FROM market.prices_eur
WHERE asset='BTC' AND d='2025-10-11' AND source='coingecko_eod';
```

---

## ⚙️ Maintenance & Indexes

| Type            | Index                              |
| --------------- | ---------------------------------- |
| Primary key     | `(asset, d, source)`               |
| Secondary index | `idx_prices_asset_date (asset, d)` |

```sql
ANALYZE market.prices_eur;
VACUUM (ANALYZE) market.prices_eur;
```

---

## 🧩 Notes & Best Practices

- Keep **one consistent source** (e.g., `'coingecko_eod'`) unless testing alternates (Kraken, Binance, etc.).
- Always use **UTC dates** for `d` to align with balances.
- `EUR` itself should have `price_eur = 1` for every date (acts as a baseline asset).
- When seeding manually, prefer end-of-day prices for consistency.
- The schema supports **multiple sources** per asset (via `source`), useful for comparison or redundancy.
- Ideal update frequency: **daily cron or ETL** (fetch via API → insert/upsert).

---

**Summary:**
`market.prices_eur` provides daily EUR-denominated prices per asset, enabling the system to compute historical valuations by joining with `core.balances_native`:

```sql
SELECT b.d, b.asset, b.amount_native, p.price_eur,
       b.amount_native * p.price_eur AS amount_eur
FROM core.balances_native b
JOIN market.prices_eur p
  ON b.asset = p.asset AND b.d = p.d
ORDER BY b.d, b.asset;
```
