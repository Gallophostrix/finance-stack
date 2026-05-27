# finance-stack

Self-hosted personal finance monitoring stack — ETL pipeline, PostgreSQL, and
Grafana dashboards for tracking portfolio performance across asset classes.

## Overview

Aggregates balances and cash flows from multiple sources (manual CSV, blockchain
APIs, market data) into a unified PostgreSQL database. Computes time-weighted
and money-weighted returns (TWR/MWR) and exposes everything via Grafana.

```
CSV (manual)  ─┐
Blockchain APIs─┼─► core.* (native) ─► derived.* (EUR) ─► returns_itd
CoinGecko      ─┘                              │
                                               └─► Grafana dashboards
```

## Stack

| Component   | Role                                      |
|-------------|-------------------------------------------|
| PostgreSQL  | Central store — raw, market, derived data |
| Python/ETL  | Fetch, import, derive, compute returns    |
| CoinGecko   | EOD prices for crypto assets              |
| Koios       | Cardano on-chain data                     |
| Mempool     | Bitcoin on-chain data                     |
| Routescan   | Avalanche on-chain data                   |
| XRPL RPC    | XRP on-chain data                         |
| Grafana     | Dashboards — allocation, performance, KPIs|
| Loki        | Log aggregation                           |
| Prometheus  | Metrics                                   |

## Repository structure

```
monitoring/
├── infra/
│   ├── compose.yml          # Grafana, Loki, Prometheus, Alloy
│   ├── grafana/             # Provisioning — datasources, dashboards
│   ├── loki/                # Loki config
│   └── prometheus/          # Prometheus config + scrape targets
├── finance/
│   ├── compose.yml          # PostgreSQL
│   ├── sql/                 # Schema init — init_core.sql, init_market.sql,
│   │                        #   init_derived.sql
│   ├── etl/                 # Python package
│   │   ├── common/          # db.py, http.py, logging.py
│   │   ├── dims/            # YAML → DB sync
│   │   ├── api/             # Blockchain + price fetchers
│   │   └── derive/          # EUR projection + returns
│   ├── scripts/             # CLI entrypoints (see below)
│   ├── assets/              # YAML per provider (asset definitions)
│   ├── data/
│   │   ├── balances/        # Monthly balance CSVs
│   │   └── flows/           # Monthly flow CSVs
│   └── requirements/
│       ├── base.txt
│       └── dev.txt
├── secrets/                 # gitignored
├── Makefile
└── flake.nix                # Nix devShell
```

## Database schema

Three schemas in PostgreSQL 16 :

**`core`** — raw data, native units
- `assets` — asset registry (code, class, is_active)
- `providers` — data providers
- `accounts` — accounts per provider
- `balances_native` — monthly balance snapshots (amount in native units)
- `flows_native` — cash flows (in / out / interest, native units)

**`market`** — price data
- `prices_eur` — EOD prices in EUR (CoinGecko, manual)

**`derived`** — computed data, EUR
- `balances_eur` — balances projected to EUR
- `flows_eur` — flows projected to EUR
- `returns_itd` — TWR/MWR inception-to-date (portfolio / category / asset)

### Key conventions

- Balances are recorded on the **1st of each month** = net value at that point
- A flow dated `YYYY-MM-01` occurred during that month and is reflected in the
  balance of the following month
- On-chain flows are dated to their actual transaction date (same convention
  applies — impact visible in the next monthly balance)
- Manual assets (ETFs, savings): `amount_native` = EUR value, `price_eur = 1.0`
- Crypto assets: `amount_native` = real units, `price_eur` via CoinGecko

## ETL pipeline

```
sync_dims        YAML assets/providers/accounts → core.*
import_balances  CSV → core.balances_native
import_flows     CSV → core.flows_native
fetch_prices     CoinGecko EOD → market.prices_eur
fetch_bitcoin    Mempool.space → balances + flows BTC
fetch_cardano    Koios → balances + flows ADA + native tokens
fetch_avalanche  Routescan + RPC → balances + flows AVAX
fetch_xrpl       Ripple public RPC → balances + flows XRP
derive           core.* + market.* → derived.balances_eur + derived.flows_eur
compute_returns  derived.* → derived.returns_itd (TWR/MWR ITD)
```

## Returns methodology

**TWR (Time-Weighted Return)** — measures portfolio manager performance,
independent of cash flow timing. Computed as the product of sub-period returns:

```
sub_return(t) = V_end / (V_start + flows_in_period)
TWR = ∏ (1 + sub_return) - 1
```

**MWR (Money-Weighted Return)** — measures investor return, accounts for cash
flow timing. Computed as IRR via Newton's method.

Granularity stored in `derived.returns_itd`:
- `portfolio` — global TWR + MWR
- `category` — per asset class (crypto / actions / epargne), TWR as
  weighted average of individual asset TWRs
- `asset` — per asset TWR

## Usage

### Prerequisites

- Nix with flakes enabled, or Python 3.11+ with `requirements/base.txt`
- Docker + Docker Compose
- `secrets/postgres_pwd.txt` — PostgreSQL password (gitignored)

### Setup

```bash
# Start PostgreSQL (schema applied automatically via init scripts)
docker compose -f finance/compose.yml up -d

# Start observability stack
docker compose -f infra/compose.yml up -d

# Enter dev environment (Nix)
nix develop
```

### Makefile targets

```
make sync-dims                           Sync YAML definitions → DB
make import-balances DATE=YYYY-MM-DD \
                     FILE=path/to.csv   Import monthly balances
make import-flows    DATE=YYYY-MM-DD \
                     FILE=path/to.csv   Import monthly flows
make fetch-prices                        Fetch EOD prices (CoinGecko)
make fetch-crypto                        Fetch all blockchains
make fetch-bitcoin/cardano/avalanche/xrpl  Individual blockchain fetch
make derive                              Project native → EUR
make derive-from DATE=YYYY-MM-DD         Re-project from a specific date
make returns                             Compute TWR/MWR
make monthly                             Full monthly workflow
make full-refresh                        Sync + fetch + derive + returns
```

Append `DRY_RUN=1` to any target to run without DB writes.

### Monthly workflow

Each month:
1. Fill `finance/data/balances/YYYY-MM-01.csv` with account balances
2. Fill `finance/data/flows/YYYY-MM-01.csv` with manual flows (ETF purchases,
   savings movements, etc.)
3. Run `make monthly`

## Asset classes

| Class    | Description                        |
|----------|------------------------------------|
| crypto   | Cryptocurrency holdings            |
| actions  | Equities — ETFs (PEA + AV)         |
| epargne  | Savings accounts + euro fund       |
| immo     | Real estate (net equity)           |

## CI

GitHub Actions (`.github/workflows/ci.yml`):
- **python** — ruff lint on `etl/`
- **yaml** — yamllint on workflows, compose files, grafana/loki/prometheus
  configs, asset definitions
- **sql** — smoke test: applies init SQL files against a fresh PostgreSQL 16
  instance
