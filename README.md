Here’s a **clean first draft of your `README.md` in English**, with a focus on **`compose.yml`**:

```markdown
# 📈 Finance Monitoring Stack

A **self-hosted** stack to **track and analyze your portfolio** (crypto, stocks, cash) with  
**PostgreSQL** as the data store, **Python ETL** jobs for data collection and performance metrics,  
and **Grafana** for visualization.  
Everything is orchestrated via **Docker Compose** for easy deployment and reproducibility.

---

## ⚙️ Stack Overview (`compose.yml`)

The stack is defined in [`compose.yml`](./compose.yml):

| Service             | Main Role                                                              | Ports (loopback)       |
|---------------------|-------------------------------------------------------------------------|------------------------|
| **finance-db**      | PostgreSQL database initialized with [init.sql](finance/sql/init.sql)  | — (internal network)   |
| **finance-etl-auto**| Python ETL to fetch **balances & flows** from BTC and Cardano APIs     | —                      |
| **finance-etl-mwr** | Python ETL to compute **MWR/TWR performance metrics**                  | —                      |
| **grafana**         | Dashboard UI connected to PostgreSQL                                   | `127.0.0.1:3000`       |
| **prometheus**      | Metrics collector                                                      | `127.0.0.1:9090`       |
| **loki**            | Centralized log aggregator                                             | `127.0.0.1:3100`       |
| **alloy**           | Grafana Alloy agent for scraping metrics and logs                      | `127.0.0.1:12345`      |

🔑 **Secrets** such as `grafana_pwd.txt` and `postgres_pwd.txt` are mounted as **Docker secrets**  
and must **not** be committed to Git.

---

## 📂 Repository Layout

```

.
├─ compose.yml                      # Full stack definition
├─ .gitignore                        # Excludes secrets, credentials, volumes, logs
├─ README.md                         # Project documentation
├─ finance/
│  ├─ sql/
│  │  └─ init.sql                    # DB schema and Grafana-friendly views
│  ├─ scripts/
│  │  ├─ auto/                        # Auto ETL (balances & flows)
│  │  │  ├─ adapters/                 # Chain-specific API code (BTC, ADA)
│  │  │  ├─ loaders/                  # DB insert/update logic
│  │  │  ├─ utils/                     # Shared utilities
│  │  │  ├─ etl_auto.py                # Main ETL entrypoint
│  │  │  └─ requirements_auto.txt      # Python deps for auto ETL
│  │  └─ mwr/                          # ETL for MWR/TWR metrics
│  │     ├─ etl_mwr.py                  # MWR calculation logic
│  │     └─ requirements_mwr.txt        # Python deps for MWR ETL
└─ grafana/                            # Provisioning of dashboards and datasources

````

---

## 🚀 Quick Start

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd monitoring
````

2. Provide your secrets:

   ```bash
   echo 'secure-password' > postgres_pwd.txt
   echo 'secure-password' > grafana_pwd.txt
   ```

3. Start the stack:

   ```bash
   docker compose up -d
   ```

4. Run the ETL jobs:

   ```bash
   docker compose run --rm finance-etl-auto
   docker compose run --rm finance-etl-mwr
   ```

---

## 📊 Notes

* The **ETL jobs** automatically fetch token balances (BTC, ADA, etc.),
  compute EUR valuations via CoinGecko, and detect monthly flows.
* The **MWR ETL** computes Money-Weighted Returns per asset and category.
* PostgreSQL is initialized automatically on first start using [`init.sql`](finance/sql/init.sql).
* Grafana dashboards use the database views defined in `init.sql`.

---

## ⚠️ Security

* **Do not commit any secrets or credentials** to the repository.
* Use `.gitignore` to exclude `*_pwd.txt`, local data volumes, and environment-specific configs.
* All external access is currently loopback-only (`127.0.0.1`) for local testing.

---

## 🗺️ Roadmap

* Add more chains (e.g. XRP, ETH) via new adapters
* CI/CD pipeline for automatic testing and deployment
* Improved alerting and notification system in Grafana
