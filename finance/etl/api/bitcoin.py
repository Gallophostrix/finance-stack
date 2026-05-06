"""
Fetch Bitcoin balances and flows from Mempool.space public API.

Endpoints:
  GET /api/address/{address}          → current balance
  GET /api/address/{address}/txs      → transactions (25/page, paginated)
  GET /api/address/{address}/txs/chain/{last_txid} → next page

Balance: confirmed only (unconfirmed excluded).
Flow uid format: bitcoin:{txid}:{account_id}:{vout_index}:{kind}
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from psycopg2.extensions import connection as PGConnection

from etl.common.http import HttpClient, HttpError

log = logging.getLogger("root")

BASE_URL = "https://mempool.space/api"
SAT_TO_BTC = Decimal("1e-8")


# ---------- DB helpers ----------


def _get_btc_accounts(conn: PGConnection) -> list[tuple[int, str]]:
    """Returns [(account_id, address)] for all active BTC accounts."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, a.external_identifier
            FROM core.accounts a
            JOIN core.providers p USING (provider_id)
            WHERE p.provider_name = 'bitcoin'
              AND a.is_active = TRUE
        """)
        return cur.fetchall()


def _last_flow_date(conn: PGConnection, account_id: int) -> Optional[date]:
    """Returns the most recent flow date for this account, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(d) FROM core.flows_native
            WHERE account_id = %s AND asset = 'BTC'
        """,
            (account_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _upsert_balance(
    conn: PGConnection,
    account_id: int,
    cut_date: date,
    amount_btc: Decimal,
) -> None:
    sql = """
    INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at)
    VALUES (%s, %s, 'BTC', %s, NOW())
    ON CONFLICT (d, account_id, asset) DO UPDATE
      SET amount_native = EXCLUDED.amount_native,
          observed_at   = EXCLUDED.observed_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, (cut_date, account_id, amount_btc))
    conn.commit()


def _upsert_flows(
    conn: PGConnection,
    rows: list[tuple],
) -> tuple[int, int]:
    sql = """
    INSERT INTO core.flows_native
      (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
    VALUES (%s, %s, %s, 'BTC', %s, %s, %s)
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


# ---------- Mempool.space fetch ----------


def _fetch_balance(client: HttpClient, address: str) -> Decimal:
    """Returns confirmed balance in BTC."""
    data = client.get_json(f"{BASE_URL}/address/{address}")
    funded = data["chain_stats"]["funded_txo_sum"]
    spent = data["chain_stats"]["spent_txo_sum"]
    sat = funded - spent
    btc = Decimal(str(sat)) * SAT_TO_BTC
    log.info(
        "btc_balance_fetched", extra={"address": address[:12] + "...", "btc": str(btc)}
    )
    return btc


def _fetch_txs(
    client: HttpClient,
    address: str,
    since_date: Optional[date] = None,
) -> list[dict]:
    """
    Fetch all confirmed transactions for address.
    If since_date provided, stops fetching when tx date < since_date.
    Returns raw tx list, newest first.
    """
    txs = []
    url = f"{BASE_URL}/address/{address}/txs"
    last_id = None

    while True:
        if last_id:
            url = f"{BASE_URL}/address/{address}/txs/chain/{last_id}"

        try:
            page = client.get_json(url)
        except HttpError as e:
            log.error(
                "btc_txs_fetch_failed",
                extra={
                    "address": address[:12] + "...",
                    "status": e.status,
                    "error": str(e),
                },
            )
            break

        if not page:
            break

        for tx in page:
            # Skip unconfirmed
            if not tx.get("status", {}).get("confirmed"):
                continue

            ts = tx["status"].get("block_time", 0)
            tx_d = datetime.fromtimestamp(ts, tz=timezone.utc).date()

            if since_date and tx_d < since_date:
                log.info(
                    "btc_txs_reached_cutoff",
                    extra={"address": address[:12] + "...", "cutoff": str(since_date)},
                )
                return txs

            txs.append(tx)

        if len(page) < 25:
            break  # last page

        last_id = page[-1]["txid"]

    log.info(
        "btc_txs_fetched", extra={"address": address[:12] + "...", "count": len(txs)}
    )
    return txs


def _parse_flows(
    txs: list[dict],
    address: str,
    account_id: int,
) -> list[tuple]:
    """
    Parse raw transactions into flow rows.
    Returns list of (flow_uid, d, account_id, amount_btc, kind, txid).
    """
    rows = []
    for tx in txs:
        txid = tx["txid"]
        ts = tx["status"]["block_time"]
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()

        # Incoming: vouts to our address
        for i, vout in enumerate(tx.get("vout", [])):
            if vout.get("scriptpubkey_address") == address:
                amount = Decimal(str(vout["value"])) * SAT_TO_BTC
                uid = f"bitcoin:{txid}:{account_id}:{i}:in"
                rows.append((uid, d, account_id, amount, "in", txid))

        # Outgoing: vins from our address
        for i, vin in enumerate(tx.get("vin", [])):
            prev = vin.get("prevout", {})
            if prev.get("scriptpubkey_address") == address:
                amount = Decimal(str(prev["value"])) * SAT_TO_BTC
                uid = f"bitcoin:{txid}:{account_id}:{i}:out"
                rows.append((uid, d, account_id, amount, "out", txid))

    return rows


# ---------- Main ----------


def run(conn: PGConnection, cut_date: Optional[date] = None) -> dict:
    """
    Fetch balances and flows for all BTC accounts.
    cut_date: date for balance snapshot (default: today's month start)
    """
    client = HttpClient(max_rps=2.0)
    today = date.today()
    cut_date = cut_date or date(today.year, today.month, 1)

    accounts = _get_btc_accounts(conn)
    if not accounts:
        log.warning("btc_no_accounts")
        return {}

    log.info(
        "btc_run_start", extra={"accounts": len(accounts), "cut_date": str(cut_date)}
    )

    total_balances = 0
    total_inserted = 0
    total_skipped = 0

    try:
        for account_id, address in accounts:
            log.info(
                "btc_processing",
                extra={"account_id": account_id, "address": address[:12] + "..."},
            )

            # Balance
            btc = _fetch_balance(client, address)
            _upsert_balance(conn, account_id, cut_date, btc)
            total_balances += 1

            # Flows — only since last known date
            since = _last_flow_date(conn, account_id)
            txs = _fetch_txs(client, address, since_date=since)
            flows = _parse_flows(txs, address, account_id)

            if flows:
                ins, skp = _upsert_flows(conn, flows)
                total_inserted += ins
                total_skipped += skp
                log.info(
                    "btc_flows_upserted",
                    extra={"account_id": account_id, "inserted": ins, "skipped": skp},
                )

    finally:
        client.close()

    result = {
        "balances": total_balances,
        "flows_inserted": total_inserted,
        "flows_skipped": total_skipped,
    }
    log.info("btc_run_done", extra=result)
    return result
