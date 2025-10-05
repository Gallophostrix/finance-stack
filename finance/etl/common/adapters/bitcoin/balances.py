# etl/common/adapters/bitcoin/balances.py
from __future__ import annotations
from decimal import Decimal
from typing import Dict, Tuple, Optional

from etl.utils.logging import setup_json_logging
from etl.utils.http import HttpClient
from .api import addr_payload, SYMBOL
from .utils import (
    COINGECKO_ID,
    DEFAULT_BASE,
    DECIMALS,
    sats_to_btc,
    get_addrs,
)


def fetch_balances(cfg_chain: dict, *, logger=None, http: Optional[HttpClient]=None)\
        -> Tuple[Dict[str, Decimal], Dict[str, dict]]:
    """
    Fetch balances and metadata for BTC from BlockCypher.
    Args:
      cfg_chain: Parsed YAML configuration for the Bitcoin chain
      logger:    Optional logger (if None, a default JSON logger is created)
      http:      Optional shared HttpClient (if None, a new one is created and closed at the end)
    Returns:
      balances = { "BTC": Decimal(qty_btc) }
      meta     = { "BTC": {"pricing":"auto","coingecko_id":"bitcoin","decimals":8} }
    """
    log = logger or setup_json_logging()

    src = cfg_chain.get("source", {}) or {}
    base_url = src.get("base_url", DEFAULT_BASE).rstrip("/")
    token = src.get("token") or None
    min_confs = src.get("min_confs")
    addrs = get_addrs(cfg_chain)

    httpc = http or HttpClient(logger=log)
    total_sats = 0
    for addr in addrs:
        payload = addr_payload(httpc, base_url, addr, token, min_confs)
        # final_balance is used for confirmed balances, balance for unconfirmed
        sats = int(payload.get("final_balance", 0))
        total_sats += sats
        log.info("btc_addr_balance", extra={
            "job":"etl-api",
            "step":"fetch_balances",
            "asset":SYMBOL,
            "addr_tail": addr[-6:],
            "sats": sats
        })

    qty_btc = sats_to_btc(total_sats)
    balances = {SYMBOL: qty_btc}
    pricing = (cfg_chain.get("assets") or [{}])[0].get("pricing", "auto")
    meta = {SYMBOL: {"pricing": pricing, "coingecko_id": COINGECKO_ID, "decimals": DECIMALS}}
    log.info("btc_balances_done", extra={
        "job":"etl-api",
        "step":"aggregate",
        "asset":SYMBOL,
        "qty_btc":str(qty_btc)
    })
    if http is None:
        httpc.close()
    # Balance always returned, even if 0.0 (to overwrite old values in DB)
    return balances, meta