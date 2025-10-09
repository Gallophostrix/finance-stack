# etl/common/loaders/batching.py
from __future__ import annotations
from typing import Iterable, Sequence, Any, List, Tuple
from psycopg2.extensions import cursor  # type: ignore


def chunked(iterable: Iterable[Any], size: int) -> Iterable[List[Any]]:
    """
    Yield successive lists (chunks) of length <= size from the iterable.
    Useful to avoid sending giant lists to the DB in one go.
    """
    buf: List[Any] = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def bulk_upsert_balances(
    cur: cursor,
    rows: Sequence[Tuple],  # (d, account_id, asset, amount_native)
    *,
    chunk_size: int = 500
) -> int:
    """
    Bulk upsert into core.balances_native.
    Returns the total number of rows processed.
    """
    sql = """
        INSERT INTO core.balances_native (d, account_id, asset, amount_native)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (d, account_id, asset) DO UPDATE
          SET amount_native = EXCLUDED.amount_native
    """

    total = 0
    for batch in chunked(rows, chunk_size):
        cur.executemany(sql, batch)
        total += len(batch)
    return total


def bulk_upsert_flows(
    cur: cursor,
    rows: Sequence[Tuple],  # (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
    *,
    chunk_size: int = 500
) -> int:
    """
    Bulk upsert into core.flows_native.
    Returns the total number of rows processed.
    """
    sql = """
        INSERT INTO core.flows_native (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (flow_uid) DO UPDATE
          SET amount_native = EXCLUDED.amount_native,
              kind = EXCLUDED.kind,
              origin_ref = EXCLUDED.origin_ref
    """

    total = 0
    for batch in chunked(rows, chunk_size):
        cur.executemany(sql, batch)
        total += len(batch)
    return total