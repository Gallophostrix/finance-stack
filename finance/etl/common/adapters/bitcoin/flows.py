# etl/common/adapters/bitcoin/flows.py
from __future__ import annotations
from datetime import date
from typing import List, Optional

from etl.utils.logging import setup_json_logging
from etl.utils.http import HttpClient
from .api import SYMBOL
from .utils import (
    CATEGORY,
    SUBCATEGORY,
    DEFAULT_BASE,
    sats_to_btc,
    get_addrs,
    parse_iso_utc_to_date
)

def detect_flows(cfg_chain: dict, start_date: date, end_date: date, *,
                 logger=None, http: Optional[HttpClient]=None, page_limit: int = 50) -> List[dict]:
    """
    Detect BTC flows (in/out) per address in the window [start_date, end_date].
    Args:
      cfg_chain: Parsed YAML configuration for the Bitcoin chain
      start_date: Start date (inclusive)
      end_date:   End date (inclusive)
      logger:     Optional logger (if None, a default JSON logger is created)
      http:       Optional shared HttpClient (if None, a new one is created and closed at the end)
      page_limit: Max number of txrefs per address page (default 50, max 200)
    
    Returns:
        flows = { "d": date,"category": CATEGORY, "subcategory": SUBCATEGORY, "asset": SYMBOL,
                  "amount_native": Decimal, "kind": "in"|"out", "tx_hash": str|None }
    """
    log = logger or setup_json_logging()

    base_url = (cfg_chain.get("provider", {}) or {}).get("base_url") or DEFAULT_BASE
    token = (cfg_chain.get("provider", {}) or {}).get("token") or None
    min_confs = (cfg_chain.get("provider", {}) or {}).get("min_confs")
    addrs = get_addrs(cfg_chain)

    httpc = http or HttpClient(logger=log)
    flows: List[dict] = []

    for addr in addrs:
        params = {"limit": page_limit}
        if token:
            params["token"] = token
        if min_confs is not None:
            params["confirmations"] = int(min_confs)
        url = f"{base_url.rstrip('/')}/addrs/{addr}"
        data = httpc.get_json(url, params=params)

        txrefs = (data.get("txrefs", []) or [])
        if not txrefs:
            continue

        # Verify if the data is truncated
        if len(txrefs) >= page_limit:
            oldest = parse_iso_utc_to_date(txrefs[-1].get("confirmed")) if txrefs[-1].get("confirmed") else None
            if oldest and oldest > start_date:
                log.warning("btc_flows_truncated",
                            extra={
                                "job":"etl-api",
                                "step":"flows_page",
                                "asset":SYMBOL,
                                "addr_tail": addr[-6:],
                                "page_limit":page_limit,
                                "oldest_on_page":str(oldest),
                                "start_date":str(start_date)
                            })

        for tx in txrefs:
            d = parse_iso_utc_to_date(tx["confirmed"])
            if d < start_date:
                # Oldest tx reached, stop processing further (txrefs are in descending order)
                break
            if d > end_date:
                continue

            amt = sats_to_btc(int(tx.get("value", 0)))
            kind = "in" if tx.get("tx_input_n", -1) == -1 else "out"

            flows.append({
                "d": d,
                "category": CATEGORY,
                "subcategory": SUBCATEGORY,
                "asset": SYMBOL,
                "amount_native": amt,
                "kind": kind,
                "tx_hash": tx.get("tx_hash"),
            })

    log.info("btc_flows_addr_done",
             extra={
                "job":"etl-api",
                "step":"flows",
                "asset":SYMBOL,
                "rows": len(flows)
            })

    if http is None:
        httpc.close()
    return flows