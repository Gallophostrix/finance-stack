"""
Fetch Cardano balances and flows from Koios public API.

Stake key based — aggregates all addresses under a stake key.
Handles ADA + native assets (INDY, METERA, iUSD, NIGHT, etc.)

Endpoints (POST, JSON body):
  /account_info   → ADA balance + rewards
  /account_assets → native token balances
  /account_txs    → transaction list (paginated via offset)

Flow uid format:
  cardano:{tx_hash}:{account_id}:{asset}:{kind}
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from psycopg2.extensions import connection as PGConnection

from etl.common.http import HttpClient, HttpError

log = logging.getLogger("root")

BASE_URL = "https://api.koios.rest/api/v1"
LOVELACE = Decimal("1e-6")  # 1 ADA = 1_000_000 lovelace
PAGE_SIZE = 1000


# ---------- DB helpers ----------


def _get_cardano_accounts(conn: PGConnection) -> list[tuple[int, str]]:
    """Returns [(account_id, stake_key)] for all active Cardano accounts."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, a.external_identifier
            FROM core.accounts a
            JOIN core.providers p USING (provider_id)
            WHERE p.provider_name = 'cardano'
              AND a.is_active = TRUE
        """)
        return cur.fetchall()


def _last_flow_date(conn: PGConnection, account_id: int) -> Optional[date]:
    """Returns the most recent flow date for this account."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(d) FROM core.flows_native
            WHERE account_id = %s
        """,
            (account_id,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _upsert_balance(
    conn: PGConnection,
    account_id: int,
    asset: str,
    cut_date: date,
    amount: Decimal,
) -> None:
    sql = """
    INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at)
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (d, account_id, asset) DO UPDATE
      SET amount_native = EXCLUDED.amount_native,
          observed_at   = EXCLUDED.observed_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, (cut_date, account_id, asset, amount))
    conn.commit()


def _upsert_flows(conn: PGConnection, rows: list[tuple]) -> tuple[int, int]:
    sql = """
    INSERT INTO core.flows_native
      (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
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


# ---------- Koios fetch ----------


def _post(client: HttpClient, endpoint: str, body: dict) -> list:
    """POST to Koios endpoint, returns parsed JSON list."""
    return client.post_json(
        f"{BASE_URL}/{endpoint}",
        body=body,
        headers={"Accept": "application/json"},
    )


def _fetch_ada_balance(
    client: HttpClient,
    stake_key: str,
) -> Decimal:
    """Returns total ADA balance (liquid + rewards) in ADA."""
    data = _post(client, "account_info", {"_stake_addresses": [stake_key]})
    if not data:
        log.warning(
            "cardano_account_not_found", extra={"stake_key": stake_key[:20] + "..."}
        )
        return Decimal("0")

    info = data[0]
    total_balance = int(info.get("total_balance", 0) or 0)
    total = Decimal(str(total_balance)) * LOVELACE

    log.info(
        "cardano_ada_balance",
        extra={"stake_key": stake_key[:20] + "...", "ada": str(total)},
    )
    return total


def _fetch_native_assets(
    client: HttpClient,
    stake_key: str,
    policy_map: dict[str, str],
) -> dict[str, Decimal]:
    """
    Returns {asset_code: amount} for known native assets.
    policy_map: {policy_id: asset_code}
    """
    data = _post(client, "account_assets", {"_stake_addresses": [stake_key]})
    if not data:
        return {}

    result: dict[str, Decimal] = {}
    for item in data:
        policy_id = item.get("policy_id", "")
        asset_name = item.get("asset_name", "")
        full_id = policy_id + asset_name
        asset_code = policy_map.get(full_id)
        if not asset_code:
            continue
        quantity = Decimal(str(item.get("quantity", 0)))
        decimals = 6  # all our Cardano assets use 6 decimals
        amount = quantity / Decimal(str(10**decimals))
        result[asset_code] = result.get(asset_code, Decimal("0")) + amount
        log.info(
            "cardano_native_balance", extra={"asset": asset_code, "amount": str(amount)}
        )

    return result


def _fetch_txs(
    client: HttpClient,
    stake_key: str,
    since_date: Optional[date] = None,
) -> list[dict]:
    """Fetch transactions for stake key, newest first, stopping at since_date."""
    txs = []
    offset = 0

    while True:
        # Pagination via query string, body contains only the stake address
        url = f"{BASE_URL}/account_txs?offset={offset}&limit={PAGE_SIZE}&order=block_time.desc"
        body = {"_stake_address": stake_key, "_after_block_height": 0}

        try:
            data = client.post_json(
                url, body=body, headers={"Accept": "application/json"}
            )
        except HttpError as e:
            log.error(
                "cardano_txs_fetch_failed",
                extra={
                    "stake_key": stake_key[:20] + "...",
                    "status": e.status,
                    "error": str(e),
                },
            )
            break

        if not data:
            break

        for tx in data:
            ts = tx.get("block_time", 0)
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if since_date and d < since_date:
                log.info(
                    "cardano_txs_reached_cutoff",
                    extra={
                        "stake_key": stake_key[:20] + "...",
                        "cutoff": str(since_date),
                    },
                )
                return txs
            tx["_date"] = d
            txs.append(tx)

        if len(data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    log.info(
        "cardano_txs_fetched",
        extra={"stake_key": stake_key[:20] + "...", "count": len(txs)},
    )
    return txs


def _fetch_tx_details(
    client: HttpClient,
    tx_hash: str,
) -> Optional[dict]:
    """Fetch full transaction details including inputs/outputs."""

    # Fetch transaction timestamp
    info = _post(client, "tx_info", {"_tx_hashes": [tx_hash]})

    # Fetch transaction details
    utxos = _post(client, "tx_utxos", {"_tx_hashes": [tx_hash]})
    if not info or not utxos:
        return None
    result = utxos[0]
    result["tx_timestamp"] = info[0].get("tx_timestamp", 0)
    return result


def _parse_flows_from_tx(
    tx_detail: dict,
    stake_key: str,
    account_id: int,
    policy_map: dict[str, str],
) -> list[tuple]:
    """
    Parse a full tx into flow rows for our stake key.
    Returns list of (flow_uid, d, account_id, asset, amount, kind, tx_hash).
    """
    rows = []
    tx_hash = tx_detail.get("tx_hash", "")
    ts = tx_detail.get("tx_timestamp", 0)
    d = datetime.fromtimestamp(ts, tz=timezone.utc).date()

    # ADA flows
    ada_in = Decimal("0")
    ada_out = Decimal("0")

    for inp in tx_detail.get("inputs", []):
        if inp.get("stake_addr") == stake_key:
            ada_out += Decimal(str(inp.get("value", 0))) * LOVELACE

    for out in tx_detail.get("outputs", []):
        if out.get("stake_addr") == stake_key:
            ada_in += Decimal(str(out.get("value", 0))) * LOVELACE

    ada_net = ada_in - ada_out
    print(f"tx={tx_hash[:16]} in={ada_in:.3f} out={ada_out:.3f} net={ada_net:.3f}")
    if ada_net > 0:
        uid = f"cardano:{tx_hash}:{account_id}:ADA:in"
        rows.append((uid, d, account_id, "ADA", ada_net, "in", tx_hash))
    elif ada_net < 0:
        uid = f"cardano:{tx_hash}:{account_id}:ADA:out"
        rows.append((uid, d, account_id, "ADA", -ada_net, "out", tx_hash))

    # Native asset flows
    for out in tx_detail.get("outputs", []):
        if out.get("stake_addr") != stake_key:
            continue
        for asset_item in out.get("asset_list", []):
            policy_id = asset_item.get("policy_id", "")
            asset_name = asset_item.get("asset_name", "")
            full_id = policy_id + asset_name
            asset_code = policy_map.get(full_id)
            if not asset_code:
                continue
            quantity = Decimal(str(asset_item.get("quantity", 0)))
            amount = quantity / Decimal("1e6")
            uid = f"cardano:{tx_hash}:{account_id}:{asset_code}:in"
            rows.append((uid, d, account_id, asset_code, amount, "in", tx_hash))

    return rows


# ---------- Main ----------


def run(
    conn: PGConnection,
    policy_map: dict[str, str],
    cut_date: Optional[date] = None,
) -> dict:
    """
    Fetch balances and flows for all Cardano accounts.

    policy_map: {policy_id: asset_code} — built from YAML files
    cut_date:   balance snapshot date (default: 1st of current month)
    """
    client = HttpClient(max_rps=1.0)
    today = date.today()
    cut_date = cut_date or date(today.year, today.month, 1)

    accounts = _get_cardano_accounts(conn)
    if not accounts:
        log.warning("cardano_no_accounts")
        return {}

    log.info(
        "cardano_run_start",
        extra={"accounts": len(accounts), "cut_date": str(cut_date)},
    )

    total_balances = 0
    total_inserted = 0
    total_skipped = 0

    try:
        for account_id, stake_key in accounts:
            log.info(
                "cardano_processing",
                extra={"account_id": account_id, "stake_key": stake_key[:20] + "..."},
            )

            # ADA balance
            ada = _fetch_ada_balance(client, stake_key)
            _upsert_balance(conn, account_id, "ADA", cut_date, ada)
            total_balances += 1

            # Native asset balances
            native = _fetch_native_assets(client, stake_key, policy_map)
            for asset_code, amount in native.items():
                _upsert_balance(conn, account_id, asset_code, cut_date, amount)
                total_balances += 1

            # Flows
            since = _last_flow_date(conn, account_id)
            txs = _fetch_txs(client, stake_key, since_date=since)

            for tx in txs:
                tx_hash = tx.get("tx_hash")
                if not tx_hash:
                    continue
                detail = _fetch_tx_details(client, tx_hash)
                if not detail:
                    continue
                flows = _parse_flows_from_tx(detail, stake_key, account_id, policy_map)
                if flows:
                    ins, skp = _upsert_flows(conn, flows)
                    total_inserted += ins
                    total_skipped += skp

    finally:
        client.close()

    result = {
        "balances": total_balances,
        "flows_inserted": total_inserted,
        "flows_skipped": total_skipped,
    }
    log.info("cardano_run_done", extra=result)
    return result
