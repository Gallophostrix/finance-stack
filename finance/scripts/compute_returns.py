#!/usr/bin/env python3
"""
Compute TWR and MWR returns (ITD) and store in derived.returns_itd.

Usage:
  python scripts/compute_returns.py
  python scripts/compute_returns.py --dry-run
"""

import argparse
import sys

from etl.common.db import connect
from etl.common.logging import setup_logging
from etl.derive.returns import _portfolio_snapshots, run

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute portfolio returns")
    ap.add_argument(
        "--dry-run", action="store_true", help="Show data summary without computing"
    )
    args = ap.parse_args()

    conn = connect()

    if args.dry_run:
        snaps = _portfolio_snapshots(conn)
        if snaps:
            log.info(
                "dry_run_summary",
                extra={
                    "from": str(snaps[0][0]),
                    "to": str(snaps[-1][0]),
                    "periods": len(snaps),
                    "v_start": str(snaps[0][1]),
                    "v_end": str(snaps[-1][1]),
                },
            )
        else:
            log.warning("dry_run_no_data")
        conn.close()
        sys.exit(0)

    try:
        run(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
