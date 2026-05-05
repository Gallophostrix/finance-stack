# etl/common/loaders/dims/ensure.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, Tuple, List

from psycopg2.extensions import connection as PGConnection  # type: ignore

from etl.utils.db import connect_with_retry
from etl.utils.logging import setup_json_logging

from .parsers import load_yaml, parse_provider, parse_assets, parse_accounts
from .sql import upsert_provider, upsert_asset, upsert_account


def ensure_from_dir(
    assets_dir: str,
    *,
    logger=None,
    dsn: str | None = None,
    ) -> Dict[Tuple[str, str, str], int]:
    """
    Scan all *.yml|*.yaml in assets_dir, parse & validate them, then upsert
    providers/assets/accounts in a single DB transaction. Build and return a
    resolver map: (provider_name, account_type, external_identifier) -> account_id.

    Error handling policy (non-strict per-file, fail at end):
      - For each invalid YAML file, emit ONE JSON error line and skip it.
      - After scanning all files, if any error occurred, raise SystemExit(1) (no traceback).
      - If no error: proceed with DB upserts and return the resolver map.
    """
    log = logger or setup_json_logging()

    root = Path(assets_dir)
    if not root.exists() or not root.is_dir():
        log.error(
            "ensure_dims_bad_assets_dir",
            extra={"step": "ensure_dims", "dir": assets_dir, "detail": "not found or not a directory"},
        )
        raise SystemExit(1)

    files: List[Path] = sorted([p for p in root.iterdir() if p.suffix.lower() in (".yml", ".yaml")])
    log.info(
        "ensure_dims_start",
        extra={
            "step": "ensure_dims",
            "files": len(files)
            }
        )

    if not files:
        log.warning(
            "ensure_dims_no_yaml_found",
            extra={
                "step": "ensure_dims",
                "rows": 0
                }
            )
        return {}

    # Phase 1: Parse/validate all YAMLs (no DB yet).
    parsed_ok = []  # list of tuples: (provider, asset_specs, account_specs)
    errors = 0

    for path in files:
        try:
            doc = load_yaml(path)
            provider = parse_provider(doc)
            asset_specs = parse_assets(doc, provider)
            account_specs = parse_accounts(doc)
            parsed_ok.append((provider, asset_specs, account_specs))
        except Exception as e:
            # One JSON error per faulty file; skip this file.
            log.error(
                "ensure_dims_yaml_invalid",
                extra={"step": "ensure_dims", "file": str(path), "msg": str(e)},
            )
            errors += 1

    # If any YAML was invalid, fail now (no DB writes).
    if errors:
        log.error(
            "ensure_dims_failed_summary",
            extra={"step": "ensure_dims", "files": len(files), "processed": len(parsed_ok), "errors": errors},
        )
        raise SystemExit(1)

    # Phase 2: Upsert all parsed specs in a single transaction.
    resolver: Dict[Tuple[str, str, str], int] = {}
    total_providers = total_assets = total_accounts = 0

    _dsn = dsn
    conn: PGConnection = connect_with_retry(_dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                for provider, asset_specs, account_specs in parsed_ok:
                    provider_id = upsert_provider(cur, provider)
                    total_providers += 1

                    for spec in asset_specs:
                        upsert_asset(cur, spec)
                        total_assets += 1

                    for spec in account_specs:
                        account_id = upsert_account(cur, provider_id, spec)
                        resolver[(provider.provider_name, spec.account_type, spec.external_identifier)] = account_id
                        total_accounts += 1

        log.info(
            "ensure_dims_done",
            extra={
                "step": "ensure_dims",
                "rows": total_providers + total_assets + total_accounts,
                "files": len(files),
                "processed": len(parsed_ok),
                "errors": 0,
            },
        )
        return resolver

    finally:
        try:
            conn.close()
        except Exception:
            pass
