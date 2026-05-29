"""
Fetch XRP balances and flows from XRPL public JSON-RPC.

Endpoint: https://s1.ripple.com:51234
1 XRP = 1,000,000 drops

Flow uid format: xrpl:{tx_hash}:{account_id}:{kind}
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from psycopg import Connection as PGConnection

from etl.common.http import HttpClient, HttpError

log = logging.getLogger("root")

BASE_URL = "https://s1.ripple.com:51234"
DROPS = Decimal("1e-6")


# ---------- DB helpers ----------


def _get_xrpl_accounts(conn: PGConnection) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, a.external_identifier
            FROM core.accounts a
            JOIN core.providers p USING (provider_id)
            WHERE p.provider_name = 'xrpl' AND a.is_active = TRUE
        """)
        return cur.fetchall()


def _last_flow_date(conn: PGConnection, account_id: int) -> Optional[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(d) FROM core.flows_native
            WHERE account_id = %s AND asset = 'XRP'
        """,
            (account_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _upsert_balance(conn, account_id, cut_date, amount) -> None:
    sql = """
    INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at)
    VALUES (%s, %s, 'XRP', %s, NOW())
    ON CONFLICT (d, account_id, asset) DO UPDATE
      SET amount_native = EXCLUDED.amount_native,
          observed_at   = EXCLUDED.observed_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, (cut_date, account_id, amount))
    conn.commit()


def _upsert_flows(conn, rows: list[tuple]) -> tuple[int, int]:
    sql = """
    INSERT INTO core.flows_native
      (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
    VALUES (%s, %s, %s, 'XRP', %s, %s, %s)
    ON CONFLICT (flow_uid) DO NOTHING
    """
    inserted, skipped = 0, 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, row)
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
    conn.commit()
    return inserted, skipped


# ---------- XRPL fetch ----------


def _rpc(client: HttpClient, method: str, params: dict) -> dict:
    data = client.post_json(
        BASE_URL,
        body={"method": method, "params": [params]},
        headers={"Content-Type": "application/json"},
    )
    result = data.get("result", {})
    if result.get("status") == "error":
        raise HttpError("POST", BASE_URL, 400, str(result.get("error_message")))
    return result


def _fetch_balance(client: HttpClient, address: str) -> Decimal:
    result = _rpc(
        client, "account_info", {"account": address, "ledger_index": "validated"}
    )
    drops = int(result["account_data"]["Balance"])
    xrp = Decimal(str(drops)) * DROPS
    log.info(
        "xrpl_balance_fetched", extra={"address": address[:12] + "...", "xrp": str(xrp)}
    )
    return xrp


def _fetch_txs(
    client: HttpClient,
    address: str,
    since_date: Optional[date] = None,
) -> list[dict]:
    txs = []
    marker = None

    while True:
        params = {
            "account": address,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": 200,
            "forward": False,  # newest first
        }
        if marker:
            params["marker"] = marker

        result = _rpc(client, "account_tx", params)
        page = result.get("transactions", [])

        for tx_wrapper in page:
            tx = tx_wrapper.get("tx", {})
            meta = tx_wrapper.get("meta", {})

            # Skip failed transactions
            if meta.get("TransactionResult") != "tesSUCCESS":
                continue

            ts = tx.get("date")
            if isinstance(ts, str):
                d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            elif isinstance(ts, int):
                # Ripple epoch: seconds since 2000-01-01
                ripple_epoch = 946684800
                d = datetime.fromtimestamp(ts + ripple_epoch, tz=timezone.utc).date()
            else:
                continue

            if since_date and d < since_date:
                log.info(
                    "xrpl_txs_reached_cutoff",
                    extra={"address": address[:12] + "...", "cutoff": str(since_date)},
                )
                return txs

            tx["_date"] = d
            tx["_meta"] = meta
            txs.append(tx)

        marker = result.get("marker")
        if not marker:
            break

    log.info(
        "xrpl_txs_fetched", extra={"address": address[:12] + "...", "count": len(txs)}
    )
    return txs


def _parse_flows(
    txs: list[dict],
    address: str,
    account_id: int,
) -> list[tuple]:
    rows = []
    for tx in txs:
        if tx.get("TransactionType") != "Payment":
            continue

        tx_hash = tx.get("hash", "")
        d = tx["_date"]
        meta = tx.get("_meta", {})

        delivered = meta.get("delivered_amount") or meta.get("DeliveredAmount")
        if not delivered or not isinstance(delivered, str):
            continue  # not XRP payment

        amount = Decimal(str(delivered)) * DROPS

        if tx.get("Destination") == address:
            uid = f"xrpl:{tx_hash}:{account_id}:in"
            rows.append((uid, d, account_id, amount, "in", tx_hash))
        elif tx.get("Account") == address:
            uid = f"xrpl:{tx_hash}:{account_id}:out"
            rows.append((uid, d, account_id, amount, "out", tx_hash))

    return rows


# ---------- Main ----------


def run(conn: PGConnection, cut_date: Optional[date] = None) -> dict:
    client = HttpClient(max_rps=2.0)
    today = date.today()
    cut_date = cut_date or date(today.year, today.month, 1)

    accounts = _get_xrpl_accounts(conn)
    if not accounts:
        log.warning("xrpl_no_accounts")
        return {}

    log.info(
        "xrpl_run_start", extra={"accounts": len(accounts), "cut_date": str(cut_date)}
    )

    total_balances = 0
    total_inserted = 0
    total_skipped = 0

    try:
        for account_id, address in accounts:
            log.info(
                "xrpl_processing",
                extra={"account_id": account_id, "address": address[:12] + "..."},
            )

            xrp = _fetch_balance(client, address)
            _upsert_balance(conn, account_id, cut_date, xrp)
            total_balances += 1

            since = _last_flow_date(conn, account_id)
            txs = _fetch_txs(client, address, since_date=since)
            flows = _parse_flows(txs, address, account_id)

            if flows:
                ins, skp = _upsert_flows(conn, flows)
                total_inserted += ins
                total_skipped += skp
                log.info(
                    "xrpl_flows_upserted",
                    extra={"account_id": account_id, "inserted": ins, "skipped": skp},
                )

    finally:
        client.close()

    result = {
        "balances": total_balances,
        "flows_inserted": total_inserted,
        "flows_skipped": total_skipped,
    }
    log.info("xrpl_run_done", extra=result)
    return result
