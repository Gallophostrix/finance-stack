# etl/api/convert.py
# Comments in English, concise.
import argparse
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import DefaultDict, Dict, List, Optional, Tuple

from etl.common.loaders import upsert_prices_eur
from etl.utils.db import connect_with_retry
from etl.utils.http import HttpClient
from etl.utils.logging import setup_json_logging
from psycopg2.extensions import connection as PGConnection  # type: ignore

SOURCE = "coingecko_eod"

EUR_NATIVE_ASSETS = {
    "COMPTE_COURANT",
    "ESPECES",
    "FONDS_EURO",
    "LIVRETS",
    "WORLD",
    "NASDAQ",
    "EMERGING",
}

# ---------- DB queries ----------


def _needed_asset_dates(conn: PGConnection) -> List[Tuple[str, date]]:
    """
    Build the set of (asset, d) we must price, based on Layer A facts,
    excluding already-present prices for SOURCE.
    """
    sql = """
    WITH needed AS (
      SELECT asset, d FROM core.balances_native
      UNION
      SELECT asset, d FROM core.flows_native
    )
    SELECT n.asset, n.d
    FROM needed n
    LEFT JOIN market.prices_eur p
      ON p.asset = n.asset AND p.d = n.d AND p.source = %s
    WHERE p.asset IS NULL
    ORDER BY n.d, n.asset;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (SOURCE,))
        return [(a, d) for a, d in cur.fetchall()]


def _asset_to_coingecko_map(conn: PGConnection) -> Dict[str, str]:
    """
    Map asset_code -> coingecko_id for active assets only.
    """
    sql = """
      SELECT asset_code, coingecko_id
      FROM core.assets
      WHERE is_active = TRUE AND coingecko_id IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return {a: cg for a, cg in cur.fetchall()}


# ---------- Coingecko calls ----------


def _round8(x: Decimal) -> Decimal:
    # Keep 8 decimals, round half-up to fit NUMERIC(20,8)
    return x.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _fetch_eod_price_eur(
    client, coingecko_id: str, dates_needed: List[date]
) -> Dict[date, Decimal]:
    if not dates_needed:
        return {}

    start_d = min(dates_needed)
    end_d = max(dates_needed)

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

    data = client.get_json(
        f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart/range",
        params={"vs_currency": "eur", "from": str(start_ts), "to": str(end_ts)},
    )

    per_day = {}
    for ms_ts, price in data.get("prices", []):
        d = datetime.utcfromtimestamp(ms_ts / 1000).date()
        per_day[d] = _round8(Decimal(str(price)))

    return {d: per_day[d] for d in dates_needed if d in per_day}


def _fetch_eod_prices_range(
    client: HttpClient,
    coingecko_id: str,
    dates_needed: List[date],
) -> Dict[date, Decimal]:
    """
    Call /coins/{id}/market_chart/range?vs_currency=eur&from=...&to=...
    Returns last EUR price per day needed.
    """
    if not dates_needed:
        return {}

    # Build inclusive [from,to] covering all needed days, UTC boundaries.
    start_d = min(dates_needed)
    end_d = max(dates_needed)
    start_ts = int(
        datetime(
            start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=timezone.utc
        ).timestamp()
    )
    # include end day to 23:59:59
    end_ts = int(
        (
            datetime(
                end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc
            )
        ).timestamp()
    )

    data = client.get_json(
        f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart/range",
        params={"vs_currency": "eur", "from": str(start_ts), "to": str(end_ts)},
    )

    # Coingecko returns: {"prices": [[ms_ts, price], ...], ...}
    prices = data.get("prices") or []
    # Build last price per UTC calendar day.
    per_day: Dict[date, Decimal] = {}
    for ms_ts, price in prices:
        try:
            dt = datetime.utcfromtimestamp(ms_ts / 1000.0).date()
            per_day[dt] = _round8(
                Decimal(str(price))
            )  # last value seen for that day wins
        except Exception:
            continue

    # Filter to only the needed dates
    return {d: v for d, v in per_day.items() if d in dates_needed}


# ---------- Orchestrator ----------


def run(
    *, dsn: Optional[str] = None, max_rps: float = 3.0, range_threshold: int = 5
) -> int:
    """
    range_threshold: if an asset has >= this many missing dates, use range endpoint (1 call) instead of history (N calls).
    """
    log = setup_json_logging()
    conn = connect_with_retry(dsn)
    client = HttpClient(logger=log, max_rps=max_rps)

    try:
        needed = _needed_asset_dates(conn)
        if not needed:
            log.info("prices_nothing_to_do", extra={"step": "prices", "rows": 0})
            return 0

        cgmap = _asset_to_coingecko_map(conn)

        # Group needed dates by asset
        needed_by_asset: DefaultDict[str, List[date]] = defaultdict(list)
        for asset, d in needed:
            needed_by_asset[asset].append(d)

        rows: List[Tuple[str, date, str, Decimal]] = []  # (asset, d, source, price)

        for asset, dates_list in sorted(needed_by_asset.items(), key=lambda kv: kv[0]):
            # Baseline EUR = 1.0
            if asset == "EUR" or asset in EUR_NATIVE_ASSETS:
                for d in dates_list:
                    rows.append((asset, d, "manual_euro", Decimal("1.0")))
                continue

            cg_id = cgmap.get(asset)
            if not cg_id:
                log.warning(
                    "price_missing_coingecko_id",
                    extra={"asset": asset, "missing_dates": len(dates_list)},
                )
                continue

            # Small set → history per day; larger set → range single call
            if len(dates_list) < range_threshold:
                if len(dates_list) <= range_threshold:
                    try:
                        per_day = _fetch_eod_price_eur(client, cg_id, dates_list)
                    except Exception:
                        per_day = _fetch_eod_prices_range(client, cg_id, dates_list)
                else:
                    per_day = _fetch_eod_prices_range(client, cg_id, dates_list)
                for d in dates_list:
                    price = per_day.get(d)
                    if price is None:
                        continue
                    rows.append((asset, d, SOURCE, price))

            else:
                per_day = _fetch_eod_prices_range(client, cg_id, dates_list)
                missing = 0
                for d in dates_list:
                    p = per_day.get(d)
                    if p is None:
                        missing += 1
                        continue
                    rows.append((asset, d, SOURCE, p))
                if missing:
                    log.warning(
                        "range_prices_incomplete",
                        extra={
                            "asset": asset,
                            "missing": missing,
                            "total": len(dates_list),
                        },
                    )

        written = upsert_prices_eur(conn, rows, logger=log) if rows else 0
        log.info("prices_job_done", extra={"step": "prices", "rows": written})
        return written

    finally:
        client.close()
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="ETL - Prices loader (Layer B)")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides PG_DSN)")
    ap.add_argument(
        "--max-rps", type=float, default=3.0, help="Coingecko max requests per second"
    )
    ap.add_argument(
        "--range-threshold",
        type=int,
        default=5,
        help="Use range API when >= N dates per asset",
    )
    args = ap.parse_args()
    run(dsn=args.dsn, max_rps=args.max_rps, range_threshold=args.range_threshold)


if __name__ == "__main__":
    main()
