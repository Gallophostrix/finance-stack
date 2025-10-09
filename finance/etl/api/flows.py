# etl/api/flows.py
from __future__ import annotations
import argparse
from datetime import date
from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Any, Optional
from decimal import Decimal

from psycopg2.extensions import connection as PGConnection  # type: ignore

from etl.utils.logging import setup_json_logging
from etl.utils.db import connect_with_retry
from etl.utils.dates import today_utc_date, last_month_utc_date

from etl.common.loaders.dims.ensure import ensure_from_dir
from etl.common.loaders.dims.parsers import load_yaml, parse_provider

from etl.common.loaders.validation import validate_flows_rows
from etl.common.loaders.upsert import upsert_flows_native, make_flow_uid

from etl.common.adapters import (
    btc_detect_flows,
    ada_detect_flows,
)


def _iter_asset_yaml_files(assets_dir: str) -> Iterable[Path]:
    root = Path(assets_dir)
    for p in sorted(root.iterdir()):
        if p.suffix.lower() in (".yml", ".yaml"):
            yield p


def _pick_flows_adapter(provider_type: str, provider_name: str):
    registry = {
        ("blockchain", "bitcoin"): btc_detect_flows,
        ("blockchain", "cardano"): ada_detect_flows,
    }
    return registry.get((provider_type, provider_name))


def _coerce_decimal(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def run(
    assets_dir: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    *,
    dsn: Optional[str] = None,
) -> int:
    """
    Orchestrate flows ingestion (Layer A) with NEW per-account format only.
    """
    log = setup_json_logging()

    # By default window: 1st of last month until now (UTC) if not provided
    today = today_utc_date()
    if start_date is None or end_date is None:
        start_date = start_date or last_month_utc_date()
        end_date = end_date or today

    # 1) Resolve accounts (dims)
    resolver = ensure_from_dir(assets_dir, logger=log)

    # 2) DB
    conn: PGConnection = connect_with_retry(dsn)
    if conn is None:
        log.error(
            "dsn_db_connection_failed",
            extra={
                "step": "flows",
                "dsn": dsn
            }
        )
        return 0

    total_rows_written = 0

    try:
        # 3) YML files
        for path in _iter_asset_yaml_files(assets_dir):
            raw = load_yaml(path)
            doc: Dict[str, Any] = dict(raw)  # Using dict() to get a mutable copy
            prov = parse_provider(doc)

            adapter = _pick_flows_adapter(prov.provider_type, prov.provider_name)
            if not adapter:
                log.info(
                    "flows_provider_skipped",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name,
                        "reason": "no_adapter"
                    }
                )
                continue

            # 4) Fetch per-account flows
            try:
                per_account_rows = adapter(doc, start_date, end_date, logger=log)
            except Exception as e:
                log.error(
                    "flows_adapter_failed",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name,
                        "msg": str(e)
                    }
                )
                continue

            if not per_account_rows:
                log.info(
                    "flows_empty",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 5) Account resolution + prepare tuples
            tuples: List[Tuple[str, date, int, str, Decimal, str, Optional[str]]] = []
            missing_accounts = 0
            bad_rows = 0

            for i, r in enumerate(per_account_rows):
                if not isinstance(r, dict):
                    bad_rows += 1
                    continue

                d = r.get("d")
                at = (r.get("account_type") or "").strip()
                eid = (r.get("external_id") or "").strip()
                asset = (r.get("asset") or "").strip().upper()
                amt = r.get("amount_native")
                kind = (r.get("kind") or "").strip()
                txh = (r.get("tx_hash") or "").strip() or None

                # Required elements
                if not isinstance(d, date) or not at or not eid or not asset or amt is None or kind not in {"in", "out"}:
                    bad_rows += 1
                    continue

                key = (prov.provider_name, at, eid)
                account_id = resolver.get(key)
                if account_id is None:
                    missing_accounts += 1
                    continue

                try:
                    amount = _coerce_decimal(amt)
                except Exception:
                    bad_rows += 1
                    continue

                origin_ref = txh
                flow_uid = make_flow_uid(prov.provider_name, account_id, origin_ref or "nohash", asset, kind)
                tuples.append((flow_uid, d, account_id, asset, amount, kind, origin_ref))

            if missing_accounts:
                log.warning(
                    "flows_missing_accounts",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name,
                        "missing": missing_accounts
                    }
                )
            if bad_rows:
                log.warning(
                    "flows_bad_rows",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name,
                        "bad_rows": bad_rows
                    }
                )

            if not tuples:
                log.info(
                    "flows_empty_after_resolve",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 6) Validation
            valid_rows, errors = validate_flows_rows(tuples, allow_zero=False, max_date=end_date)
            for e in errors:
                log.error(
                    "validation_flow_failed",
                    extra={
                        "step": "validate_flows",
                        "index": e.index,
                        "reason": e.reason
                    }
                )
            if not valid_rows:
                log.warning(
                    "flows_all_invalid",
                    extra={
                        "step": "flows",
                        "provider": prov.provider_name
                    }
                )
                continue

            # 7) Upsert
            try:
                written = upsert_flows_native(conn, valid_rows, logger=log)
                total_rows_written += written
            except Exception as e:
                log.error(
                    "flows_upsert_failed",
                    extra={
                        "step": "upsert_flows",
                        "provider": prov.provider_name,
                        "msg": str(e)
                    }
                )
                continue

        log.info(
            "flows_job_done",
            extra={
                "step": "flows",
                "rows": total_rows_written
            }
        )
        return total_rows_written

    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="ETL - Flows Orchestrator (new format only)")
    ap.add_argument("assets_dir", help="Directory with provider YAML files (e.g., finance/assets)")
    ap.add_argument("--start", help="Start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="End date YYYY-MM-DD (inclusive)")
    ap.add_argument("--dsn", help="PostgreSQL DSN (overrides PG_DSN env var)", default=None)
    args = ap.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    run(args.assets_dir, start_date=start, end_date=end, dsn=args.dsn)


if __name__ == "__main__":
    main()
