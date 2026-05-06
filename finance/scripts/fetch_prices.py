#!/usr/bin/env python3
"""
Fetch missing EOD prices from CoinGecko and populate market.prices_eur.

Usage:
  python scripts/fetch_prices.py
  python scripts/fetch_prices.py --dry-run
"""

import argparse
import sys

from etl.api.coingecko import load_api_key, run, seed_manual_prices
from etl.common.db import connect
from etl.common.logging import setup_logging

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch EOD prices from CoinGecko")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched, no DB writes",
    )
    args = ap.parse_args()

    try:
        api_key = load_api_key()
        log.info("api_key_loaded", extra={"source": "env/file"})
    except RuntimeError as e:
        log.error("api_key_missing", extra={"error": str(e)})
        sys.exit(1)

    if args.dry_run:
        log.info("dry_run_mode", extra={"note": "no DB writes"})
        conn = connect()
        from etl.api.coingecko import _needed_asset_dates

        needed, cg_map = _needed_asset_dates(conn)
        for asset, dates in sorted(needed.items()):
            log.info(
                "dry_run_asset",
                extra={
                    "asset": asset,
                    "dates": len(dates),
                    "cg_id": cg_map.get(asset, "manual_euro"),
                },
            )
        conn.close()
        sys.exit(0)

    conn = connect()
    try:
        seed_manual_prices(conn)
        written = run(conn, api_key)
        log.info("fetch_prices_done", extra={"rows": written})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
