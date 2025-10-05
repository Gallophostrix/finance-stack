# etl/common/loaders/pg.py
from __future__ import annotations

from typing import Iterable, Dict, Any, Tuple
from decimal import Decimal
from datetime import date, datetime
import itertools

from etl.utils.db import get_conn  # <- your existing DB connector (psycopg2/psycopg)
from etl.utils.logging import setup_json_logging

ALLOWED_KINDS = {"in", "out", "fee", "interest"}

# ---------------------------
# Small utility helpers
# ---------------------------

def _chunked(iterable: Iterable[Any], n: int) -> Iterable[list]:
    """Yield lists of size n (last chunk may be smaller)."""
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, n))
        if not chunk:
            break
        yield chunk


def _as_dt(x: Any) -> datetime:
    """Normalize datetime inputs; if None -> now (UTC)."""
    if isinstance(x, datetime):
        return x
    return datetime.utcnow()


# ---------------------------
# Validation helpers
# ---------------------------

def _validate_balance_row(r: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Expected fields:
      d: date (UTC day)
      account_id: int
      asset: str (must exist in core.assets)
      amount_native: Decimal >= 0
      observed_at: datetime (optional)
      source: str (optional)
    """
    if not isinstance(r.get("d"), date):
        return False, "invalid_d"
    if not isinstance(r.get("account_id"), int):
        return False, "invalid_account_id"
    if not isinstance(r.get("asset"), str) or not r["asset"]:
        return False, "invalid_asset"
    amt = r.get("amount_native")
    if not isinstance(amt, (Decimal, int, float)) or Decimal(amt) < 0:
        return False, "invalid_amount_native"
    return True, ""


def _validate_flow_row(r: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Expected fields:
      flow_uid: str (unique, idempotence key)
      d: date (UTC day)
      account_id: int
      asset: str
      amount_native: Decimal > 0
      kind: str in {'in','out','fee','interest'}
      origin_ref: str|None
      origin_pos: int|None
      confirmations: int|None
      seen_at: datetime (optional)
      source: str|None
      meta: dict|None
    """
    if not isinstance(r.get("flow_uid"), str) or not r["flow_uid"]:
        return False, "invalid_flow_uid"
    if not isinstance(r.get("d"), date):
        return False, "invalid_d"
    if not isinstance(r.get("account_id"), int):
        return False, "invalid_account_id"
    if not isinstance(r.get("asset"), str) or not r["asset"]:
        return False, "invalid_asset"
    amt = r.get("amount_native")
    if not isinstance(amt, (Decimal, int, float)) or Decimal(amt) <= 0:
        return False, "invalid_amount_native"
    if r.get("kind") not in ALLOWED_KINDS:
        return False, "invalid_kind"
    conf = r.get("confirmations")
    if conf is not None and (not isinstance(conf, int) or conf < 0):
        return False, "invalid_confirmations"
    pos = r.get("origin_pos")
    if pos is not None and not isinstance(pos, int):
        return False, "invalid_origin_pos"
    return True, ""


# ---------------------------
# Upsert BALANCES (core.balances_native)
# ---------------------------

def upsert_balances_native(
    rows: Iterable[Dict[str, Any]],
    *,
    chunk_size: int = 1000,
    logger=None,
    conn=None
) -> Dict[str, int]:
    """
    Insert-or-update balances in core.balances_native.
    - Natural key: (d, account_id, asset)
    - Update only if NEW.observed_at > OLD.observed_at
    Returns counters: {'inserted':..., 'updated':..., 'skipped':..., 'rejected':...}
    """
    log = logger or setup_json_logging()
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "rejected": 0}

    # Validate & normalize upfront
    prepared = []
    for r in rows:
        ok, reason = _validate_balance_row(r)
        if not ok:
            stats["rejected"] += 1
            log.warning("balances_rejected", extra={"reason": reason, "row": str(r)[:500]})
            continue
        prepared.append({
            "d": r["d"],
            "account_id": int(r["account_id"]),
            "asset": r["asset"],
            "amount_native": Decimal(r["amount_native"]),
            "observed_at": r.get("observed_at") or _as_dt(None),
            "source": r.get("source"),
        })

    if not prepared:
        return stats

    own_conn = None
    try:
        own_conn = get_conn() if conn is None else None
        cx = conn or own_conn
        with cx:
            with cx.cursor() as cur:
                sql = """
                INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (d, account_id, asset)
                DO UPDATE SET
                    amount_native = EXCLUDED.amount_native,
                    observed_at   = EXCLUDED.observed_at,
                    source        = COALESCE(EXCLUDED.source, core.balances_native.source)
                WHERE EXCLUDED.observed_at > core.balances_native.observed_at
                RETURNING (xmax = 0) AS inserted,  -- inserted row if true
                          (xmax <> 0) AND (EXCLUDED.observed_at > core.balances_native.observed_at) AS updated;
                """
                for chunk in _chunked(prepared, chunk_size):
                    args = [
                        (r["d"], r["account_id"], r["asset"], r["amount_native"], r["observed_at"], r["source"])
                        for r in chunk
                    ]
                    cur.executemany(sql, args)
                    # Count inserts/updates from RETURNING flags
                    for flag_inserted, flag_updated in cur.fetchall():
                        if flag_inserted:
                            stats["inserted"] += 1
                        elif flag_updated:
                            stats["updated"] += 1
                        else:
                            stats["skipped"] += 1
        log.info("balances_upsert_done", extra=stats)
        return stats
    finally:
        if own_conn is not None:
            own_conn.close()


# ---------------------------
# Upsert FLOWS (core.flows_native)
# ---------------------------

def upsert_flows_native(
    rows: Iterable[Dict[str, Any]],
    *,
    chunk_size: int = 1000,
    logger=None,
    conn=None
) -> Dict[str, int]:
    """
    Insert-or-update flows in core.flows_native.
    - Idempotence key: flow_uid (PRIMARY KEY)
    - Update only if "newer" (confirmations increased OR seen_at newer OR amount changed)
    Returns counters: {'inserted':..., 'updated':..., 'skipped':..., 'rejected':...}
    """
    log = logger or setup_json_logging()
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "rejected": 0}

    prepared = []
    for r in rows:
        ok, reason = _validate_flow_row(r)
        if not ok:
            stats["rejected"] += 1
            log.warning("flows_rejected", extra={"reason": reason, "row": str(r)[:500]})
            continue
        prepared.append({
            "flow_uid": r["flow_uid"],
            "d": r["d"],
            "account_id": int(r["account_id"]),
            "asset": r["asset"],
            "amount_native": Decimal(r["amount_native"]),
            "kind": r["kind"],
            "origin_ref": r.get("origin_ref"),
            "origin_pos": r.get("origin_pos"),
            "confirmations": r.get("confirmations"),
            "seen_at": r.get("seen_at") or _as_dt(None),
            "source": r.get("source"),
            "meta": r.get("meta"),
        })

    if not prepared:
        return stats

    own_conn = None
    try:
        own_conn = get_conn() if conn is None else None
        cx = conn or own_conn
        with cx:
            with cx.cursor() as cur:
                sql = """
                INSERT INTO core.flows_native
                  (flow_uid, d, account_id, asset, amount_native, kind, origin_ref, origin_pos, confirmations, seen_at, source, meta)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (flow_uid)
                DO UPDATE SET
                  -- Keep the most informative / freshest data
                  amount_native = EXCLUDED.amount_native,                -- amount at this logical position should be consistent
                  kind          = EXCLUDED.kind,                         -- if kind changed we align (rare)
                  origin_ref    = COALESCE(EXCLUDED.origin_ref, core.flows_native.origin_ref),
                  origin_pos    = COALESCE(EXCLUDED.origin_pos, core.flows_native.origin_pos),
                  confirmations = GREATEST(COALESCE(EXCLUDED.confirmations,0), COALESCE(core.flows_native.confirmations,0)),
                  seen_at       = CASE
                                     WHEN EXCLUDED.seen_at > core.flows_native.seen_at THEN EXCLUDED.seen_at
                                     ELSE core.flows_native.seen_at
                                  END,
                  source        = COALESCE(EXCLUDED.source, core.flows_native.source),
                  meta          = COALESCE(EXCLUDED.meta, core.flows_native.meta)
                WHERE
                  -- Only update if something actually got "newer" or different
                  (EXCLUDED.amount_native IS DISTINCT FROM core.flows_native.amount_native)
                  OR (COALESCE(EXCLUDED.confirmations,0) > COALESCE(core.flows_native.confirmations,0))
                  OR (EXCLUDED.seen_at > core.flows_native.seen_at)
                RETURNING (xmax = 0) AS inserted,
                          (xmax <> 0) AS updated;  -- updated when conflict branch took effect
                """
                for chunk in _chunked(prepared, chunk_size):
                    args = [
                        (
                            r["flow_uid"], r["d"], r["account_id"], r["asset"], r["amount_native"], r["kind"],
                            r["origin_ref"], r["origin_pos"], r["confirmations"], r["seen_at"], r["source"], r["meta"]
                        )
                        for r in chunk
                    ]
                    cur.executemany(sql, args)
                    for flag_inserted, flag_updated in cur.fetchall():
                        if flag_inserted:
                            stats["inserted"] += 1
                        elif flag_updated:
                            stats["updated"] += 1
                        else:
                            stats["skipped"] += 1
        log.info("flows_upsert_done", extra=stats)
        return stats
    finally:
        if own_conn is not None:
            own_conn.close()
