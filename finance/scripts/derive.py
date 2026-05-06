#!/usr/bin/env python3
"""
Project native balances and flows to EUR.

Usage:
  python scripts/derive.py
  python scripts/derive.py --from-date 2026-01-01
  python scripts/derive.py --dry-run
"""

import argparse
import sys
from datetime import date

from etl.common.db import connect
from etl.common.logging import setup_logging
from etl.derive.project import run

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Project balances and flows to EUR")
    ap.add_argument(
        "--from-date", default=None, help="Only process dates >= YYYY-MM-DD"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Show counts without writing"
    )
    args = ap.parse_args()

    from_date = None
    if args.from_date:
        try:
            from_date = date.fromisoformat(args.from_date)
        except ValueError:
            log.error("invalid_date", extra={"date": args.from_date})
            sys.exit(1)

    conn = connect()

    if args.dry_run:
        # Count what would be projected
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM core.balances_native b
                JOIN market.prices_eur p ON p.asset = b.asset AND p.d = b.d
            """)
            bal = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM core.flows_native f
                JOIN market.prices_eur p ON p.asset = f.asset AND p.d = f.d
            """)
            flo = cur.fetchone()[0]
        log.info(
            "dry_run_counts",
            extra={"balances_projectable": bal, "flows_projectable": flo},
        )
        conn.close()
        sys.exit(0)

    try:
        run(conn, from_date=from_date)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
