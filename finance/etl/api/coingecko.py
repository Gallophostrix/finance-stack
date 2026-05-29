"""
Fetch historical EOD prices from CoinGecko Demo API.
Populates market.prices_eur for all crypto assets with a coingecko_id.

Rate limit: 30 req/min on Demo plan → max_rps=0.4 (safe margin)
Historical limit: 365 days back from today on Demo plan.

Endpoint used:
  GET /coins/{id}/market_chart/range
  params: vs_currency=eur, from=unix_ts, to=unix_ts
"""

import logging
import os
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from psycopg import Connection as PGConnection

from etl.common.http import HttpClient, HttpError

log = logging.getLogger("root")

SOURCE = "coingecko_eod"
BASE_URL = "https://api.coingecko.com/api/v3"
DEMO_LIMIT = 365  # days back from today on Demo plan

EUR_NATIVE = {
    "COMPTE_COURANT",
    "ESPECES",
    "FONDS_EURO",
    "LIVRETS",
    "WORLD",
    "NASDAQ",
    "EMERGING",
}


# ---------- API key ----------


def load_api_key() -> str:
    key_file = os.environ.get("COINGECKO_KEY_FILE")
    if key_file:
        try:
            return open(key_file).read().strip()
        except OSError as e:
            raise RuntimeError(f"Cannot read COINGECKO_KEY_FILE={key_file}: {e}") from e
    key = os.environ.get("COINGECKO_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "No CoinGecko API key found. Set COINGECKO_KEY_FILE or COINGECKO_API_KEY."
    )


# ---------- DB queries ----------


def _needed_asset_dates(
    conn: PGConnection,
) -> tuple[dict[str, list[date]], dict[str, str]]:
    """
    Returns:
      - needed  : {asset_code: [dates missing from market.prices_eur]}
      - cg_map  : {asset_code: coingecko_id}

    Only includes dates within the Demo plan limit (365 days back).
    Excludes EUR-native assets (no API call needed).
    """
    cutoff_date = date.fromordinal(date.today().toordinal() - DEMO_LIMIT)
    sql = """
    WITH needed AS (
      SELECT DISTINCT asset, d FROM core.balances_native
      UNION
      SELECT DISTINCT asset, d FROM core.flows_native
    )
    SELECT n.asset, n.d, a.coingecko_id
    FROM needed n
    JOIN core.assets a ON a.asset_code = n.asset
    LEFT JOIN market.prices_eur p
      ON p.asset = n.asset AND p.d = n.d AND p.source = %s
    WHERE a.coingecko_id IS NOT NULL
      AND a.is_active = TRUE
      AND p.asset IS NULL
      AND n.d >= %s
    ORDER BY n.asset, n.d
    """
    with conn.cursor() as cur:
        cur.execute(sql, (SOURCE, cutoff_date))
        rows = cur.fetchall()

    needed: dict[str, list[date]] = {}
    cg_map: dict[str, str] = {}
    for asset, d, cg_id in rows:
        needed.setdefault(asset, []).append(d)
        cg_map[asset] = cg_id

    log.info(
        "prices_needed",
        extra={
            "assets": len(needed),
            "total_dates": sum(len(v) for v in needed.values()),
            "cutoff": str(cutoff_date),
        },
    )
    return needed, cg_map


def _upsert_prices(
    conn: PGConnection,
    rows: list[tuple[str, date, str, Decimal]],
) -> int:
    sql = """
    INSERT INTO market.prices_eur (asset, d, source, price_eur, observed_at)
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (asset, d, source) DO UPDATE
      SET price_eur   = EXCLUDED.price_eur,
          observed_at = EXCLUDED.observed_at
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


# ---------- CoinGecko fetch ----------


def _round8(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _fetch_range(
    client: HttpClient,
    cg_id: str,
    dates: list[date],
    api_key: str,
) -> dict[date, Decimal]:
    """
    Fetch EOD prices for a list of dates using /market_chart/range.
    Returns {date: price_eur}.
    """
    if not dates:
        return {}

    start_d = min(dates)
    end_d = max(dates)

    start_ts = int(
        datetime(
            start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc
        ).timestamp()
    )
    end_ts = int(
        datetime(
            end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc
        ).timestamp()
    )

    try:
        data = client.get_json(
            f"{BASE_URL}/coins/{cg_id}/market_chart/range",
            params={"vs_currency": "eur", "from": str(start_ts), "to": str(end_ts)},
            headers={"x-cg-demo-api-key": api_key},
        )
    except HttpError as e:
        log.error(
            "coingecko_fetch_failed",
            extra={"cg_id": cg_id, "status": e.status, "error": str(e)},
        )
        return {}

    per_day: dict[date, Decimal] = {}
    for ms_ts, price in data.get("prices", []):
        d = datetime.fromtimestamp(ms_ts / 1000.0, tz=timezone.utc).date()
        per_day[d] = _round8(price)

    found = len({d for d in dates if d in per_day})
    missing = len(dates) - found
    log.info(
        "coingecko_range_fetched",
        extra={
            "cg_id": cg_id,
            "requested": len(dates),
            "found": found,
            "missing": missing,
        },
    )

    return {d: per_day[d] for d in dates if d in per_day}


# ---------- Manual fetcher ----------


def seed_manual_prices(conn: PGConnection) -> int:
    """
    Insert price_eur = 1.0 for all assets without coingecko_id
    (epargne, actions ETF, immo) for all dates present in balances/flows
    but missing from market.prices_eur.
    """
    sql = """
    INSERT INTO market.prices_eur (asset, d, source, price_eur, observed_at)
    SELECT DISTINCT n.asset, n.d, 'manual_euro', 1.0, NOW()
    FROM (
        SELECT asset, d FROM core.balances_native
        UNION
        SELECT asset, d FROM core.flows_native
    ) n
    JOIN core.assets a ON a.asset_code = n.asset
    LEFT JOIN market.prices_eur p
        ON p.asset = n.asset AND p.d = n.d AND p.source = 'manual_euro'
    WHERE a.coingecko_id IS NULL
      AND a.is_active = TRUE
      AND p.asset IS NULL
    ON CONFLICT (asset, d, source) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        count = cur.rowcount
    conn.commit()
    log.info("manual_prices_seeded", extra={"rows": count})
    return count


# ---------- Orchestrator ----------


def run(conn: PGConnection, api_key: Optional[str] = None) -> int:
    api_key = api_key or load_api_key()
    client = HttpClient(max_rps=0.4)

    try:
        needed, cg_map = _needed_asset_dates(conn)
        if not needed:
            log.info("prices_nothing_to_do", extra={"step": "coingecko"})
            return 0

        rows: list[tuple[str, date, str, Decimal]] = []

        for asset, dates in sorted(needed.items()):
            if asset in EUR_NATIVE:
                for d in dates:
                    rows.append((asset, d, "manual_euro", Decimal("1.0")))
                log.info(
                    "prices_manual_euro", extra={"asset": asset, "dates": len(dates)}
                )
                continue

            cg_id = cg_map.get(asset)
            if not cg_id:
                log.warning("prices_no_coingecko_id", extra={"asset": asset})
                continue

            prices = _fetch_range(client, cg_id, dates, api_key)
            for d, price in prices.items():
                rows.append((asset, d, SOURCE, price))

        written = _upsert_prices(conn, rows) if rows else 0
        log.info("prices_done", extra={"rows": written})
        return written

    finally:
        client.close()
