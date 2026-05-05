#!/usr/bin/env python3
"""
Sync assets/providers/accounts from YAML files into the DB.

Usage:
  python scripts/sync_dims.py --assets finance/assets/
  python scripts/sync_dims.py --assets finance/assets/ --dry-run
"""

import argparse
import sys
from pathlib import Path

from etl.common.db import connect
from etl.common.logging import setup_logging
from etl.dims.parser import parse_dir
from etl.dims.sync import sync_all


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync dims from YAML → DB")
    ap.add_argument("--assets", required=True, help="Path to assets directory")
    ap.add_argument(
        "--dry-run", action="store_true", help="Parse and validate only, no DB writes"
    )
    args = ap.parse_args()

    log = setup_logging()

    assets_dir = Path(args.assets)
    try:
        dims = parse_dir(assets_dir)
    except (FileNotFoundError, ValueError) as e:
        log.error("parse_failed", extra={"error": str(e)})
        sys.exit(1)

    if args.dry_run:
        log.info("dry_run_complete", extra={"files": len(dims), "note": "no DB writes"})
        sys.exit(0)

    conn = connect()
    try:
        sync_all(conn, dims)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
