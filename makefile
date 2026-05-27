# ─────────────────────────────────────────────────────────────────────────────
# Finance ETL — Makefile
# Usage : make <target> [DATE=2026-05-01] [FILE=finance/data/...] [DRY_RUN=1]
# ─────────────────────────────────────────────────────────────────────────────

PYTHON      := python3
SCRIPTS     := finance/scripts
ASSETS      := finance/assets
SECRETS     := secrets/postgres_pwd.txt

# Postgres password reading from secrets file
PG_PWD      := $(shell cat $(SECRETS) 2>/dev/null)
export PG_DSN := postgresql://finance:$(PG_PWD)@localhost:5432/finance

# Optional --dry-run flag: make <target> DRY_RUN=1
DRY         := $(if $(DRY_RUN),--dry-run,)

# ─────────────────────────────────────────────────────────────────────────────
# Principal targets
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help sync-dims fetch-all fetch-prices fetch-crypto \
        fetch-bitcoin fetch-cardano fetch-avalanche fetch-xrpl \
        import-balances import-flows \
        derive returns \
        monthly full-refresh

help: ## Display this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*##"}; {printf "  %-20s %s\n", $$1, $$2}'

# ─────────────────────────────────────────────────────────────────────────────
# Dimensions
# ─────────────────────────────────────────────────────────────────────────────

sync-dims: ## Sync assets/providers/accounts from YAML files
	$(PYTHON) $(SCRIPTS)/sync_dims.py --assets $(ASSETS) $(DRY)

# ─────────────────────────────────────────────────────────────────────────────
# Manual import (balances + flows CSV)
# Usage : make import-balances DATE=2026-05-01 FILE=finance/data/balances/2026-05-01.csv
# ─────────────────────────────────────────────────────────────────────────────

import-balances: ## CSV import (balances)  [DATE=, FILE=]
ifndef DATE
	$(error DATE est requis : make import-balances DATE=2026-05-01 FILE=...)
endif
ifndef FILE
	$(error FILE est requis : make import-balances DATE=2026-05-01 FILE=...)
endif
	$(PYTHON) $(SCRIPTS)/import_balances.py --date $(DATE) --file $(FILE) $(DRY)

import-flows: ## CSV import (flows)         [DATE=, FILE=]
ifndef DATE
	$(error DATE est requis : make import-flows DATE=2026-05-01 FILE=...)
endif
ifndef FILE
	$(error FILE est requis : make import-flows DATE=2026-05-01 FILE=...)
endif
	$(PYTHON) $(SCRIPTS)/import_flows.py --date $(DATE) --file $(FILE) $(DRY)

# ─────────────────────────────────────────────────────────────────────────────
# Fetch prices
# ─────────────────────────────────────────────────────────────────────────────

fetch-prices: ## Fetch EOD prices from CoinGecko
	$(PYTHON) $(SCRIPTS)/fetch_prices.py $(DRY)

# ─────────────────────────────────────────────────────────────────────────────
# Fetch crypto (individually)
# ─────────────────────────────────────────────────────────────────────────────

fetch-bitcoin: ## Fetch BTC balances and flows
	$(PYTHON) $(SCRIPTS)/fetch_bitcoin.py $(DRY)

fetch-cardano: ## Fetch Cardano balances and flows (ADA, NIGHT, INDY...)
	$(PYTHON) $(SCRIPTS)/fetch_cardano.py --assets $(ASSETS) $(DRY)

fetch-avalanche: ## Fetch Avalanche balances and flows
	$(PYTHON) $(SCRIPTS)/fetch_avalanche.py $(DRY)

fetch-xrpl: ## Fetch XRP balances and flows
	$(PYTHON) $(SCRIPTS)/fetch_xrpl.py $(DRY)

fetch-crypto: fetch-bitcoin fetch-cardano fetch-avalanche fetch-xrpl ## Fetch all crypto balances and flows


# ─────────────────────────────────────────────────────────────────────────────
# Fetch all
# ─────────────────────────────────────────────────────────────────────────────

fetch-all: fetch-prices fetch-crypto ## Fetch all crypto and fiat balances and flows

# ─────────────────────────────────────────────────────────────────────────────
# Derived pipeline
# ─────────────────────────────────────────────────────────────────────────────

derive: ## Native to EUR projection (balances + flows)
	$(PYTHON) $(SCRIPTS)/derive.py $(DRY)

derive-from: ## Projection from a given date     [DATE=2026-01-01]
ifndef DATE
	$(error DATE est requis : make derive-from DATE=2026-01-01)
endif
	$(PYTHON) $(SCRIPTS)/derive.py --from-date $(DATE) $(DRY)

returns: ## Calculates TWR/MWR ITD
	$(PYTHON) $(SCRIPTS)/compute_returns.py $(DRY)

# ─────────────────────────────────────────────────────────────────────────────
# Composed workflows
# ─────────────────────────────────────────────────────────────────────────────

monthly: fetch-all derive returns ## Complete monthly workflow (fetch + derive + returns)

full-refresh: sync-dims fetch-all derive returns ## Complete refresh from scratch
