#!/usr/bin/env python3
"""
Fetch Cardano balances and flows from Koios.

Usage:
  python scripts/fetch_cardano.py --assets finance/assets/
  python scripts/fetch_cardano.py --assets finance/assets/ --cut-date 2026-05-01
  python scripts/fetch_cardano.py --assets finance/assets/ --dry-run
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml
from etl.api.cardano import (
    _fetch_ada_balance,
    _fetch_native_assets,
    _get_cardano_accounts,
    _last_flow_date,
    run,
)
from etl.common.db import connect
from etl.common.http import HttpClient
from etl.common.logging import setup_logging

log = setup_logging()


def _build_policy_map(assets_dir: Path) -> dict[str, str]:
    """
    Parse all YAML files and build {policy_id: asset_code}
    for Cardano native assets.
    """
    policy_map: dict[str, str] = {}
    for f in assets_dir.glob("*.yml"):
        raw = yaml.safe_load(f.read_text())
        provider = raw.get("provider", {})
        if provider.get("name", "").lower() != "cardano":
            continue
        for asset in raw.get("assets", []):
            policy_id = asset.get("policy_id")
            code = asset.get("code", "").upper()
            if policy_id and code:
                policy_map[policy_id] = code
                log.info(
                    "policy_map_loaded",
                    extra={"policy_id": policy_id[:16] + "...", "asset": code},
                )
    return policy_map


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Cardano balances and flows")
    ap.add_argument("--assets", required=True, help="Path to assets directory")
    ap.add_argument("--cut-date", default=None, help="Balance snapshot date YYYY-MM-DD")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched, no DB writes",
    )
    args = ap.parse_args()

    assets_dir = Path(args.assets)
    policy_map = _build_policy_map(assets_dir)

    if not policy_map:
        log.error(
            "no_policy_map",
            extra={"error": "No Cardano policy_ids found in YAML files"},
        )
        sys.exit(1)

    log.info("policy_map_ready", extra={"entries": len(policy_map)})

    cut_date = None
    if args.cut_date:
        try:
            cut_date = date.fromisoformat(args.cut_date)
        except ValueError:
            log.error("invalid_date", extra={"date": args.cut_date})
            sys.exit(1)

    conn = connect()

    if args.dry_run:
        accounts = _get_cardano_accounts(conn)
        client = HttpClient(max_rps=1.0)
        try:
            for account_id, stake_key in accounts:
                ada = _fetch_ada_balance(client, stake_key)
                native = _fetch_native_assets(client, stake_key, policy_map)
                since = _last_flow_date(conn, account_id)
                log.info(
                    "dry_run_account",
                    extra={
                        "account_id": account_id,
                        "stake_key": stake_key[:20] + "...",
                        "ada": str(ada),
                        "native": {k: str(v) for k, v in native.items()},
                        "flows_since": str(since),
                    },
                )
        finally:
            client.close()
            conn.close()
        sys.exit(0)

    try:
        run(conn, policy_map=policy_map, cut_date=cut_date)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
