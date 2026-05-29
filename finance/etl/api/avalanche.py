"""
Fetch Avalanche C-Chain balances and flows via:
- Public RPC (api.avax.network) for balance
- Routescan API (no key required) for transactions

Flow uid format: avalanche:{tx_hash}:{account_id}:{kind}
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from psycopg import Connection as PGConnection

from etl.common.http import HttpClient, HttpError

log = logging.getLogger("root")

RPC_URL = "https://api.avax.network/ext/bc/C/rpc"
ROUTESCAN = "https://api.routescan.io/v2/network/mainnet/evm/43114/etherscan/api"
WEI = Decimal("1e-18")


# ---------- DB helpers ----------


def _get_avax_accounts(conn: PGConnection) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, a.external_identifier
            FROM core.accounts a
            JOIN core.providers p USING (provider_id)
            WHERE p.provider_name = 'avalanche' AND a.is_active = TRUE
        """)
        return cur.fetchall()


def _last_flow_date(conn: PGConnection, account_id: int) -> Optional[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(d) FROM core.flows_native
            WHERE account_id = %s AND asset = 'AVAX'
        """,
            (account_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _upsert_balance(conn, account_id, cut_date, amount) -> None:
    sql = """
    INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at)
    VALUES (%s, %s, 'AVAX', %s, NOW())
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
    VALUES (%s, %s, %s, 'AVAX', %s, %s, %s)
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


# ---------- API fetch ----------


def _fetch_balance(client: HttpClient, address: str) -> Decimal:
    data = client.post_json(
        RPC_URL,
        body={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1,
        },
        headers={"Content-Type": "application/json"},
    )
    wei = int(data["result"], 16)
    avax = Decimal(str(wei)) * WEI
    log.info(
        "avax_balance_fetched",
        extra={"address": address[:12] + "...", "avax": str(avax)},
    )
    return avax


def _fetch_txs(
    client: HttpClient,
    address: str,
    since_date: Optional[date] = None,
) -> list[dict]:
    txs = []
    page = 1
    offset = 100

    while True:
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": "0",
            "endblock": "99999999",
            "sort": "desc",
            "page": str(page),
            "offset": str(offset),
            "apikey": "placeholder",
        }
        try:
            data = client.get_json(ROUTESCAN, params=params)
        except HttpError as e:
            log.error(
                "avax_txs_fetch_failed",
                extra={
                    "address": address[:12] + "...",
                    "status": e.status,
                    "error": str(e),
                },
            )
            break

        if data.get("status") != "1":
            break

        results = data.get("result", [])
        for tx in results:
            if tx.get("isError") == "1":
                continue

            ts = int(tx["timeStamp"])
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()

            if since_date and d < since_date:
                log.info(
                    "avax_txs_reached_cutoff",
                    extra={"address": address[:12] + "...", "cutoff": str(since_date)},
                )
                return txs

            tx["_date"] = d
            txs.append(tx)

        if len(results) < offset:
            break
        page += 1

    log.info(
        "avax_txs_fetched", extra={"address": address[:12] + "...", "count": len(txs)}
    )
    return txs


def _parse_flows(
    txs: list[dict],
    address: str,
    account_id: int,
) -> list[tuple]:
    rows = []
    address = address.lower()

    for tx in txs:
        tx_hash = tx.get("hash", "")
        d = tx["_date"]
        value = int(tx.get("value", "0"))

        if value == 0:
            continue

        amount = Decimal(str(value)) * WEI

        if tx.get("to", "").lower() == address:
            uid = f"avalanche:{tx_hash}:{account_id}:in"
            rows.append((uid, d, account_id, amount, "in", tx_hash))
        elif tx.get("from", "").lower() == address:
            uid = f"avalanche:{tx_hash}:{account_id}:out"
            rows.append((uid, d, account_id, amount, "out", tx_hash))

    return rows


# ---------- Main ----------


def run(conn: PGConnection, cut_date: Optional[date] = None) -> dict:
    client = HttpClient(max_rps=3.0)
    today = date.today()
    cut_date = cut_date or date(today.year, today.month, 1)

    accounts = _get_avax_accounts(conn)
    if not accounts:
        log.warning("avax_no_accounts")
        return {}

    log.info(
        "avax_run_start", extra={"accounts": len(accounts), "cut_date": str(cut_date)}
    )

    total_balances = 0
    total_inserted = 0
    total_skipped = 0

    try:
        for account_id, address in accounts:
            log.info(
                "avax_processing",
                extra={"account_id": account_id, "address": address[:12] + "..."},
            )

            avax = _fetch_balance(client, address)
            _upsert_balance(conn, account_id, cut_date, avax)
            total_balances += 1

            since = _last_flow_date(conn, account_id)
            txs = _fetch_txs(client, address, since_date=since)
            flows = _parse_flows(txs, address, account_id)

            if flows:
                ins, skp = _upsert_flows(conn, flows)
                total_inserted += ins
                total_skipped += skp
                log.info(
                    "avax_flows_upserted",
                    extra={"account_id": account_id, "inserted": ins, "skipped": skp},
                )

    finally:
        client.close()

    result = {
        "balances": total_balances,
        "flows_inserted": total_inserted,
        "flows_skipped": total_skipped,
    }
    log.info("avax_run_done", extra=result)
    return result
