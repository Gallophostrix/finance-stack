# etl/common/adapters/bitcoin/balances.py
from __future__ import annotations
from typing import Dict, Tuple, Optional, List

from etl.utils.logging import setup_json_logging
from etl.utils.http import HttpClient
from .api import addr_payload, ASSET_CODE
from .utils import (
    COINGECKO_ID,
    DEFAULT_BASE,
    DECIMALS,
    sats_to_btc,
    get_addrs,
)


def fetch_balances(
        cfg_chain: dict,
        *,
        logger=None,
        http: Optional[HttpClient]=None
    ) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Fetch per-account BTC balances from BlockCypher.

    Args:
      cfg_chain: Parsed YAML configuration for the Bitcoin chain
      logger:    Optional logger (if None, a default JSON logger is created)
      http:      Optional shared HttpClient (if None, a new one is created and closed at the end)

    Returns:
      rows = [
        {"account_type":"address","external_id":"<addr>","asset":"BTC","amount_native":Decimal},
        ...
      ]
      meta = {"BTC":{"pricing":"auto","coingecko_id":"bitcoin","decimals":8}}
    """
    log = logger or setup_json_logging()

    src = cfg_chain.get("source", {}) or {}
    base_url = src.get("base_url", DEFAULT_BASE).rstrip("/")
    token = src.get("token") or None
    min_confs = src.get("min_confs")
    addrs = get_addrs(cfg_chain)

    httpc = http or HttpClient(logger=log)

    rows: List[dict] = []
    total_sats = 0
    try:
        for addr in addrs:
            payload = addr_payload(httpc, base_url, addr, token, min_confs)
            # final_balance is confirmed; 'balance' would include unconfirmed
            sats = int(payload.get("final_balance", 0))
            total_sats += sats

            amt_btc = sats_to_btc(sats)
            rows.append({
                "account_type": "address",
                "external_id": addr,
                "asset": ASSET_CODE,
                "amount_native": amt_btc,
            })

            log.info(
                "btc_addr_balance",
                extra={
                    "job": "etl-api",
                    "step": "fetch_balances",
                    "asset": ASSET_CODE,
                    "addr_tail": addr[-6:],
                    "sats": sats
                },
            )
    finally:
        if http is None:
            httpc.close()

    pricing = (cfg_chain.get("assets") or [{}])[0].get("pricing", "auto")
    meta = {ASSET_CODE: {"pricing": pricing, "coingecko_id": COINGECKO_ID, "decimals": DECIMALS}}
    
    log.info(
        "btc_balances_done",
        extra={
            "job": "etl-api",
            "step": "aggregate",
            "asset": ASSET_CODE,
            "qty_btc_total": str(sats_to_btc(total_sats)),
            "accounts": len(rows),
        },
    )

    # Always return rows (possibly empty) to allow DB overwrite to 0 for missing accounts
    return rows, meta