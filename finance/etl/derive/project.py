"""
Project native balances and flows to EUR using market prices.

Populates:
  derived.balances_eur  ← core.balances_native × market.prices_eur
  derived.flows_eur     ← core.flows_native    × market.prices_eur

Both are idempotent via ON CONFLICT DO UPDATE / DO NOTHING.
Missing prices are logged as warnings — rows are skipped, not failed.
"""

import logging
from datetime import date
from typing import Optional

from psycopg2.extensions import connection as PGConnection

log = logging.getLogger("root")


# ---------- balances_eur ----------


def project_balances(
    conn: PGConnection,
    from_date: Optional[date] = None,
) -> int:
    """
    Upsert derived.balances_eur for all (d, account_id, asset) in
    core.balances_native that have a matching price in market.prices_eur.

    from_date: only process dates >= from_date (default: all)
    Returns number of rows upserted.
    """
    date_filter = "AND b.d >= %(from_date)s" if from_date else ""

    sql = f"""
    INSERT INTO derived.balances_eur (d, account_id, asset, value_eur, observed_at)
    SELECT
        b.d,
        b.account_id,
        b.asset,
        ROUND(b.amount_native * p.price_eur, 2) AS value_eur,
        NOW()
    FROM core.balances_native b
    JOIN (
        -- Pick best available price source per (asset, date)
        -- Priority: coingecko_eod > manual_euro
        SELECT DISTINCT ON (asset, d)
            asset, d, price_eur
        FROM market.prices_eur
        ORDER BY asset, d,
            CASE source
                WHEN 'coingecko_eod' THEN 1
                WHEN 'manual_euro'   THEN 2
                ELSE 3
            END
    ) p ON p.asset = b.asset AND p.d = b.d
    {date_filter}
    ON CONFLICT (d, account_id, asset) DO UPDATE
        SET value_eur   = EXCLUDED.value_eur,
            observed_at = EXCLUDED.observed_at
    """
    params = {"from_date": from_date} if from_date else {}

    with conn.cursor() as cur:
        cur.execute(sql, params)
        count = cur.rowcount
    conn.commit()

    # Log missing prices
    missing_sql = f"""
    SELECT COUNT(*) FROM core.balances_native b
    LEFT JOIN market.prices_eur p ON p.asset = b.asset AND p.d = b.d
    WHERE p.asset IS NULL
    {date_filter}
    """
    with conn.cursor() as cur:
        cur.execute(missing_sql, params)
        row = cur.fetchone()
        missing = row[0] if row else 0

    if missing:
        log.warning("balances_eur_missing_prices", extra={"missing": missing})

    log.info("balances_eur_projected", extra={"rows": count, "missing_prices": missing})
    return count


# ---------- flows_eur ----------


def project_flows(
    conn: PGConnection,
    from_date: Optional[date] = None,
) -> int:
    """
    Upsert derived.flows_eur for all flows in core.flows_native
    that have a matching price in market.prices_eur.

    from_date: only process dates >= from_date (default: all)
    Returns number of rows upserted.
    """
    date_filter = "AND f.d >= %(from_date)s" if from_date else ""

    sql = f"""
    INSERT INTO derived.flows_eur
        (native_flow_uid, d, account_id, asset, amount_eur, kind, source_price, observed_at)
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
    JOIN (
        SELECT DISTINCT ON (asset, d)
            asset, d, price_eur, source
        FROM market.prices_eur
        ORDER BY asset, d,
            CASE source
                WHEN 'coingecko_eod' THEN 1
                WHEN 'manual_euro'   THEN 2
                ELSE 3
            END
    ) p ON p.asset = f.asset AND p.d = f.d
    {date_filter}
    ON CONFLICT (native_flow_uid) DO UPDATE
        SET amount_eur   = EXCLUDED.amount_eur,
            source_price = EXCLUDED.source_price,
            observed_at  = EXCLUDED.observed_at
    """
    params = {"from_date": from_date} if from_date else {}

    with conn.cursor() as cur:
        cur.execute(sql, params)
        count = cur.rowcount
    conn.commit()

    # Log missing prices
    missing_sql = f"""
    SELECT COUNT(*) FROM core.flows_native f
    LEFT JOIN market.prices_eur p ON p.asset = f.asset AND p.d = f.d
    WHERE p.asset IS NULL
    {date_filter}
    """
    with conn.cursor() as cur:
        cur.execute(missing_sql, params)
        row = cur.fetchone()
        missing = row[0] if row else 0

    if missing:
        log.warning("flows_eur_missing_prices", extra={"missing": missing})

    log.info("flows_eur_projected", extra={"rows": count, "missing_prices": missing})
    return count


# ---------- Main ----------


def run(
    conn: PGConnection,
    from_date: Optional[date] = None,
) -> dict:
    """Run both projections. Returns counts."""
    balances = project_balances(conn, from_date=from_date)
    flows = project_flows(conn, from_date=from_date)

    result = {"balances_eur": balances, "flows_eur": flows}
    log.info("derive_done", extra=result)
    return result
