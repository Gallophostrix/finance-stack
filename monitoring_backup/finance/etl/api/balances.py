# etl/api/balances.py
from __future__ import annotations
from datetime import date
from pathlib import Path
from typing import Iterable, Optional, Any, List, Tuple
import argparse

from decimal import Decimal
from psycopg2.extensions import connection as PGConnection # type: ignore

from etl.utils.logging import setup_json_logging
from etl.utils.db import connect_with_retry
from etl.utils.dates import today_utc_date

# dims loader (resolver + DB sync)
from etl.common.loaders.dims.ensure import ensure_from_dir
from etl.common.loaders.dims.parsers import load_yaml, parse_provider

# validation + upsert
from etl.common.loaders.validation import validate_balances_rows
from etl.common.loaders.upsert import upsert_balances_native

# adapters (we only wire the ones you have right now)
from etl.common.adapters import (
    btc_fetch_balances,
    ada_fetch_balances,
)


def _iter_asset_yaml_files(assets_dir: str) -> Iterable[Path]:
    root = Path(assets_dir)
    for p in sorted(root.iterdir()):
        if p.suffix.lower() in (".yml", ".yaml"):
            yield p


def _pick_balances_adapter(provider_type: str, provider_name: str):
    """
    Return a callable (cfg -> (balances, meta)) or None if no adapter.
    Extend this mapping when you add new chains/providers.
    """
    if provider_type == "blockchain" and provider_name == "bitcoin":
        return btc_fetch_balances
    if provider_type == "blockchain" and provider_name == "cardano":
        return ada_fetch_balances
    return None


def _coerce_decimal(x: Any) -> Decimal:
    # Sécurise la conversion en Decimal (évite float direct)
    return x if isinstance(x, Decimal) else Decimal(str(x))

def run(
        assets_dir: str,
        as_of: Optional[date] = None,
        *,
        dsn: Optional[str] = None
) -> int:
    """
    Orchestrate balances ingestion for all providers described in assets_dir.
    Returns the number of rows written to core.balances_native.
    """
    log = setup_json_logging()
    as_of_d = as_of or today_utc_date()

    # 1) Ensure dimensions (providers/assets/accounts) and build resolver
    resolver = ensure_from_dir(assets_dir, logger=log)

    # 2) Open DB connection once
    conn: PGConnection = connect_with_retry(dsn)
    if conn is None:
        log.error(
            "dsn_db_connection_failed",
            extra={
                "step": "balances",
                "dsn": dsn
            }
        )
        return 0

    total_rows = 0
    try:
        # 3) Loop over provider YAML files
        for path in _iter_asset_yaml_files(assets_dir):
            doc = dict(load_yaml(path))
            prov = parse_provider(doc)

            adapter = _pick_balances_adapter(prov.provider_type, prov.provider_name)

            if not adapter:
                log.info(
                    "balances_provider_skipped",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name,
                        "reason": "no_adapter"
                    }
                )
                continue

            # 4) Call adapter
            try:
                per_account_rows, _meta = adapter(doc, logger=log)
            except Exception as e:
                log.error(
                    "balances_adapter_failed",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name,
                        "msg": str(e)
                    }
                )
                continue

            if not per_account_rows:
                log.info(
                    "balances_empty",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 5) Account resolution + tuple preparation
            tuples: List[Tuple[date, int, str, Decimal]] = []
            missing_accounts = 0

            for i, r in enumerate(per_account_rows):
                if not isinstance(r, dict):
                    log.error(
                        "balances_row_invalid",
                        extra={
                            "step": "balances",
                            "provider": prov.provider_name,
                            "index": i,
                            "reason": "not_a_mapping"
                        }
                    )
                    continue

                at = (r.get("account_type") or "").strip()
                eid = (r.get("external_identifier") or "").strip()
                asset = (r.get("asset_code") or "").strip().upper()
                amt = r.get("amount_native")

                if not at or not eid or not asset or amt is None:
                    log.error(
                        "balances_row_invalid",
                        extra={
                            "step": "balances",
                            "provider": prov.provider_name,
                            "index": i,
                            "reason": "missing_fields"
                        }
                    )
                    continue

                key = (prov.provider_name, at, eid)
                account_id = resolver.get(key)
                if account_id is None:
                    missing_accounts += 1
                    continue

                try:
                    amount = _coerce_decimal(amt)
                except Exception:
                    log.error(
                        "balances_row_invalid_amount",
                        extra={
                            "step": "balances",
                            "provider": prov.provider_name,
                            "index": i,
                            "value": repr(amt)
                        }
                    )
                    continue

                tuples.append((as_of_d, account_id, asset, amount))

            if missing_accounts:
                log.warning(
                    "balances_missing_accounts",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name,
                        "missing": missing_accounts
                    }
                )

            if not tuples:
                log.info(
                    "balances_empty_after_resolve",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 6) Validate
            valid_rows, errors = validate_balances_rows(tuples, allow_zero=True, max_date=as_of_d)
            for e in errors:
                log.error(
                    "validation_balance_failed",
                    extra={
                        "step": "validate_balances",
                        "index": e.index,
                        "reason": e.reason
                    }
                )
            if not valid_rows:
                log.warning(
                    "balances_all_invalid",
                    extra={
                        "step": "balances",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 7) Upsert
            try:
                written = upsert_balances_native(conn, valid_rows, logger=log)
                total_rows += written
            except Exception as e:
                log.error(
                    "balances_upsert_failed",
                    extra={
                        "step": "upsert_balances",
                        "provider": prov.provider_name,
                        "msg": str(e)
                    }
                )
                continue

        log.info(
            "balances_job_done",
            extra={
                "step": "balances",
                "rows": total_rows
            }
        )
        return total_rows

    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="ETL - Balances Orchestrator")
    ap.add_argument("assets_dir", help="Directory containing provider YAML files (e.g., finance/assets)")
    ap.add_argument("--as-of", help="Cut date (YYYY-MM-DD). Defaults to today UTC.", default=None)
    ap.add_argument("--dsn", help="PostgreSQL DSN (overrides PG_DSN env var)", default=None)
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    run(args.assets_dir, as_of=as_of, dsn=args.dsn)


if __name__ == "__main__":
    main()
