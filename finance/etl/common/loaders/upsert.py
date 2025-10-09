# etl/common/loaders/upsert.py
from __future__ import annotations
from typing import Iterable, List, Tuple, Optional
from datetime import date
from decimal import Decimal

from psycopg2.extensions import connection as PGConnection # type: ignore

from etl.utils.logging import setup_json_logging
from .batching import bulk_upsert_balances, bulk_upsert_flows


# --------- Public helpers (IDs / keys) ---------

def make_flow_uid(
    provider_name: str,
    account_id: int,
    tx_hash: str,
    asset: str,
    kind: str
) -> str:
    """
    Build a UNIQUE idempotent flow UID.
    Compatible with schema PK core.flows_native(flow_uid).
    - We aggregate per (tx/account/asset) at adapter/orchestrator level,
      so 'io_index' is not required here.
    """
    p = provider_name.strip().lower()
    a = str(account_id)
    h = tx_hash.strip()
    s = asset.strip().upper()
    k = kind.strip().lower()
    return f"{p}:{h}:{a}:{s}:{k}"


# --------- High-level upsert entrypoints ---------

def upsert_balances_native(
    conn: PGConnection,
    rows: Iterable[Tuple[date, int, str, Decimal]],
    *,
    logger=None,
    chunk_size: int = 500,
) -> int:
    """
    Upsert balances into core.balances_native in a single transaction.

    rows: iterable of tuples (d, account_id, asset, amount_native)
      - d: date (UTC cut date)
      - account_id: int (FK to core.accounts)
      - asset: UPPER code (FK to core.assets.asset_code)
      - amount_native: Decimal (>= 0)

    Returns: total number of rows processed.
    """
    log = logger or setup_json_logging()
    total = 0
    with conn:
        with conn.cursor() as cur:
            # materialize rows once to count/log; if you prefer streaming, drop the list() and the count log
            batch_rows: List[Tuple[date, int, str, Decimal]] = list(rows)
            if not batch_rows:
                log.info(
                    "balances_upsert_noop",
                    extra={
                        "step": "upsert_balances",
                        "rows": 0
                        }
                    )
                return 0
            total = bulk_upsert_balances(cur, batch_rows, chunk_size=chunk_size)

    log.info(
        "balances_upsert_done",
        extra={
            "step": "upsert_balances",
            "rows": total
            }
        )
    return total


def upsert_flows_native(
    conn: PGConnection,
    rows: Iterable[Tuple[str, date, int, str, Decimal, str, Optional[str]]],
    *,
    logger=None,
    chunk_size: int = 500,
) -> int:
    """
    Upsert flows into core.flows_native in a single transaction.

    rows: iterable of tuples (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
      - flow_uid: str PK (use make_flow_uid(...) or your own stable scheme)
      - d: date (UTC)
      - account_id: int (FK)
      - asset: UPPER code (FK)
      - amount_native: Decimal (> 0)  [always positive; 'kind' carries direction]
      - kind: 'in' | 'out' | 'fee' | 'interest'
      - origin_ref: optional reference (tx_hash, booking id, etc.)

    Returns: total number of rows processed.
    """
    log = logger or setup_json_logging()
    total = 0
    with conn:
        with conn.cursor() as cur:
            batch_rows: List[Tuple[str, date, int, str, Decimal, str, Optional[str]]] = list(rows)
            if not batch_rows:
                log.info(
                    "flows_upsert_noop",
                    extra={
                        "step": "upsert_flows",
                        "rows": 0
                        }
                    )
                return 0
            total = bulk_upsert_flows(cur, batch_rows, chunk_size=chunk_size)

    log.info(
        "flows_upsert_done",
        extra={
            "step": "upsert_flows",
            "rows": total
            }
        )
    return total
