# finance-stack

Self-hosted personal finance monitoring — Python ETL pipeline, PostgreSQL,
Grafana dashboards. Deployed on Selene (NixOS) via a native Nix module.

## Architecture

``` text
CSV (manual)   ─┐
Blockchain APIs─┼─► core.* (native)   ─► derived.* (EUR) ─► returns_itd
CoinGecko      ─┘         │
                          └────────────────────────────► Grafana
```

Three PostgreSQL 16 schemas:

| Schema    | Role                                                        |
|-----------|--------------------------------------------------------------|
| `core`    | Dimensions (assets, providers, accounts) + native facts (balances, flows) |
| `market`  | EOD prices (`prices_eur`)                                    |
| `derived` | EUR projection + ITD returns (`returns_itd`)                 |

Versioned, numbered SQL migrations (`001_`, `002_`, `003_`), applied
automatically on boot via a `public.schema_migrations` table
(idempotent — see `finance-migrate` below).

## Stack

| Component   | Role                                       |
|-------------|--------------------------------------------|
| Nix flake   | Packaging + NixOS module (replaces Make/Docker/requirements.txt) |
| PostgreSQL 16 | Storage — native via `services.postgresql`, Unix socket only (no TCP) |
| Python 3.12 / `finance-etl` | Fetch, import, derive, returns calculation — `pyproject.toml` package |
| systemd (oneshot + timer) | Monthly pipeline orchestration |
| sops-nix    | Secrets (CoinGecko key, Grafana password if TCP) |
| CoinGecko / Koios / Mempool / Routescan / XRPL RPC | Price and on-chain data sources |
| Grafana / Loki / Prometheus / Alloy | Observability (`infra/`) |

## Repository structure

``` text
monitoring/
├── flake.nix                # Nix devShell + inputs
├── Makefile                 # OBSOLETE — see warning above
├── nix/
│   ├── package.nix          # buildPythonApplication — finance-etl package
│   └── module.nix           # services.finance NixOS module
├── finance/
│   ├── pyproject.toml       # replaces requirements/base.txt + dev.txt
│   ├── sql/                 # 001_init_core.sql, 002_init_derived.sql, 003_init_market.sql
│   ├── etl/
│   │   ├── common/          # db.py, http.py, logging.py
│   │   ├── dims/            # YAML → DB sync
│   │   ├── api/             # blockchain + price fetchers
│   │   └── derive/          # EUR projection + returns
│   ├── scripts/             # CLI entrypoints (exposed as finance-*)
│   ├── assets/              # YAML per provider (asset/account definitions)
│   ├── data/
│   │   ├── balances/        # Monthly balance CSVs
│   └   └── flows/           # Monthly flow CSVs
├── infra/
│   ├── alloy/config.alloy
│   ├── grafana/provisioning/
│   ├── loki/config.yml
│   └── prometheus/prometheus.yml
└── secrets/                  # gitignored — sops-nix
    ├── coingecko_key.txt
    ├── grafana_pwd.txt
    └── postgres_pwd.txt
```

## Nix packaging

`nix/package.nix` builds `finance-etl` via `buildPythonApplication`
(Python 3.12, `pyproject = true`):

- Filtered source: `finance/pyproject.toml`, `finance/etl`, `finance/scripts`, `finance/sql`
- Runtime deps (`propagatedBuildInputs`): `psycopg[binary]`, `httpx`, `pyyaml`, `python-dateutil`
- `pythonImportsCheck = ["etl"]` — build fails if the package is broken
- `postInstall`: copies `sql/` into `$out/lib/finance-etl/sql` (read by `finance-migrate`), and wraps `finance-sync-dims` / `finance-fetch-cardano` with `--assets <dataDir>/assets`

Exposed entrypoints (`pyproject.toml` → `[project.scripts]`):

``` text
finance-sync-dims        finance-fetch-avalanche
finance-fetch-prices     finance-fetch-xrpl
finance-fetch-bitcoin    finance-import-balances
finance-fetch-cardano    finance-import-flows
finance-derive           finance-returns
```

## NixOS module — `services.finance`

Declared in `nix/module.nix`, enabled via `services.finance.enable = true`.

### Main options

| Option | Default | Role |
|---|---|---|
| `dataDir` | `/var/lib/finance` | Mutable directory (CSV, YAML assets) |
| `user` | `finance` | Dedicated system user |
| `postgresUser` / `postgresDb` | `finance` / `finance` | PostgreSQL identity |
| `coinGeckoKeyFile` | — | Path to the sops-nix secret (CoinGecko key) |
| `grafanaSecretsFile` | — | Required only if Grafana connects over TCP (socket is enough otherwise) |
| `timerOnCalendar` | `*-*-01 00:01:00` | systemd calendar for the monthly run |

### What the module does

1. **System user** `finance` (no home, no login), member of the `postgres` group for socket access
2. **Native PostgreSQL**: `enableTCPIP = false`, `peer` auth for the `finance` user on the `finance` DB, local `trust` access for `grafana`
3. **`finance-migrate`** (`wantedBy = multi-user.target`): applies, on boot, any SQL migration not yet recorded in `schema_migrations`, then `GRANT SELECT` to `grafana` on `core`/`market`/`derived`
4. **One-shot services** per ETL command (`finance-import-balances`, `finance-fetch-bitcoin`, etc.) — **not started automatically**, launched manually:

   ```bash
   systemctl start finance-fetch-cardano
   systemctl start finance-derive
   ```

5. **`finance-monthly`**: orchestrates the full pipeline (import current month's balances/flows if the CSVs exist → fetch prices/bitcoin/cardano/avalanche/xrpl → derive → returns)
6. **`finance-monthly` timer**: triggers `finance-monthly.service` per `timerOnCalendar`, `Persistent = true` (catches up the run if the machine was off)

systemd isolation on every service: `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem = "strict"`, write access restricted to `dataDir`.

## Usage

### Initial setup

```nix
# configuration.nix or flake host
services.finance = {
  enable = true;
  coinGeckoKeyFile = config.sops.secrets.coingecko_key.path;
  # timerOnCalendar default: 1st of month at 00:01
};
```

After `nixos-rebuild switch`, `finance-migrate` applies the schema on boot.

### Monthly workflow

1. Drop `finance/data/balances/YYYY-MM-01.csv` and
   `finance/data/flows/YYYY-MM.csv` into `dataDir`
2. The timer triggers `finance-monthly` automatically on the 1st of the
   month — or manually:

   ```bash
   systemctl start finance-monthly
   journalctl -u finance-monthly -f
   ```

### Individual commands

```bash
systemctl start finance-import-balances
systemctl start finance-import-flows
systemctl start finance-fetch-prices
systemctl start finance-fetch-bitcoin
systemctl start finance-fetch-cardano
systemctl start finance-fetch-avalanche
systemctl start finance-fetch-xrpl
systemctl start finance-derive
systemctl start finance-returns
```

The `finance-*` binaries are also directly available system-wide
(`environment.systemPackages = [financeEtl]`), usable outside systemd for
debugging:

```bash
finance-derive --dry-run
```

### Dev shell

```bash
nix develop
```

## Asset classes

| Class    | Description                        |
|----------|------------------------------------|
| crypto   | Cryptocurrency holdings            |
| actions  | Equities — ETFs (PEA + AV)         |
| epargne  | Savings accounts + euro fund       |
| immo     | Real estate (net equity)           |

## Returns methodology

**TWR** (Time-Weighted Return) — performance independent of cash flow timing:

```
sub_return(t) = V_end / (V_start + flows_in_period)
TWR = ∏ (1 + sub_return) - 1
```

**MWR** (Money-Weighted Return) — investor return, IRR via Newton's method.

Granularity stored in `derived.returns_itd`: `portfolio` (global TWR+MWR),
`category` (per asset class), `asset` (per asset).

## CI

`.github/workflows/ci.yml` — needs re-checking: the `ruff` lint on `etl/`
and the SQL smoke test are probably still relevant, but the CI likely
still references `requirements/` and the old Docker pipeline for the SQL
test; needs adapting to build via `nix build .#finance-etl` and validate
migrations in `001`→`002`→`003` order.
