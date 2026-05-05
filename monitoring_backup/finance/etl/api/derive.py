# etl/api/derive.py
from __future__ import annotations
import argparse
from typing import Optional
from psycopg2.extensions import connection as PGConnection  # type: ignore

from etl.utils.db import connect_with_retry
from etl.utils.logging import setup_json_logging

# SQL CTE used by both upserts: pick one price per (asset, d)
PREFERRED_PRICE_CTE = """
WITH ranked AS (
  SELECT
    asset, d, source, price_eur,
    ROW_NUMBER() OVER (
      PARTITION BY asset, d
      ORDER BY CASE source
        WHEN 'coingecko_eod' THEN 1
        WHEN 'manual_seed'   THEN 2
        WHEN 'manual_1to1'   THEN 3
        ELSE 9
      END
    ) AS rk
  FROM market.prices_eur
),
preferred AS (
  SELECT asset, d, source, price_eur
  FROM ranked WHERE rk = 1
)
"""

BALANCES_UPSERT_SQL = PREFERRED_PRICE_CTE + """
INSERT INTO derived.balances_eur (d, account_id, asset, value_eur, observed_at)
SELECT
  b.d,
  b.account_id,
  b.asset,
  ROUND(b.amount_native * p.price_eur, 2) AS value_eur,
  NOW()
FROM core.balances_native b
JOIN preferred p
  ON p.asset = b.asset AND p.d = b.d
ON CONFLICT (d, account_id, asset) DO UPDATE
  SET value_eur = EXCLUDED.value_eur,
      observed_at = NOW();
"""

FLOWS_UPSERT_SQL = PREFERRED_PRICE_CTE + """
INSERT INTO derived.flows_eur (native_flow_uid, d, account_id, asset, amount_eur, kind, source_price, observed_at)
SELECT
  f.flow_uid,
  f.d,
  f.account_id,
  f.asset,
  ROUND(f.amount_native * p.price_eur, 2) AS amount_eur,
  f.kind,
  p.source AS source_price,
  NOW()
FROM core.flows_native f
JOIN preferred p
  ON p.asset = f.asset AND p.d = f.d
ON CONFLICT (native_flow_uid) DO UPDATE
  SET amount_eur   = EXCLUDED.amount_eur,
      source_price = EXCLUDED.source_price,
      observed_at  = NOW();
"""

def derive_balances(conn: PGConnection, logger) -> int:
    """Upsert EUR balances from Layer A × preferred prices."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(BALANCES_UPSERT_SQL)
            # rowcount is undefined for multi-row INSERT .. SELECT; return -1
    logger.info("derive_balances_done", extra={"step": "derive_balances"})
    return 0

def derive_flows(conn: PGConnection, logger) -> int:
    """Upsert EUR flows from Layer A × preferred prices."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(FLOWS_UPSERT_SQL)
    logger.info("derive_flows_done", extra={"step": "derive_flows"})
    return 0

def run(*, dsn: Optional[str] = None, which: str = "all") -> None:
    log = setup_json_logging()
    conn = connect_with_retry(dsn)
    try:
        if which in ("all", "balances"):
            derive_balances(conn, log)
        if which in ("all", "flows"):
            derive_flows(conn, log)
        log.info("derive_job_done", extra={"step": "derive", "which": which})
    finally:
        try:
            conn.close()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser(description="ETL - Layer C (derived) upserts")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides PG_DSN)")
    ap.add_argument("--which", choices=["all", "balances", "flows"], default="all",
                    help="What to derive: balances, flows, or all")
    args = ap.parse_args()
    run(dsn=args.dsn, which=args.which)

if __name__ == "__main__":
    main()
