#!/usr/bin/env python3
"""
Fetch XRP balances and flows from XRPL public JSON-RPC.

Usage:
  python scripts/fetch_xrpl.py
  python scripts/fetch_xrpl.py --cut-date 2026-05-01
  python scripts/fetch_xrpl.py --dry-run
"""

import argparse
import sys
from datetime import date

from etl.api.xrpl import _fetch_balance, _get_xrpl_accounts, _last_flow_date, run
from etl.common.db import connect
from etl.common.http import HttpClient
from etl.common.logging import setup_logging

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch XRP balances and flows")
    ap.add_argument("--cut-date", default=None, help="Balance snapshot date YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="No DB writes")
    args = ap.parse_args()

    cut_date = None
    if args.cut_date:
        try:
            cut_date = date.fromisoformat(args.cut_date)
        except ValueError:
            log.error("invalid_date", extra={"date": args.cut_date})
            sys.exit(1)

    conn = connect()

    if args.dry_run:
        accounts = _get_xrpl_accounts(conn)
        client = HttpClient(max_rps=2.0)
        try:
            for account_id, address in accounts:
                xrp = _fetch_balance(client, address)
                since = _last_flow_date(conn, account_id)
                log.info(
                    "dry_run_account",
                    extra={
                        "account_id": account_id,
                        "address": address[:12] + "...",
                        "xrp": str(xrp),
                        "flows_since": str(since),
                    },
                )
        finally:
            client.close()
            conn.close()
        sys.exit(0)

    try:
        run(conn, cut_date=cut_date)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
