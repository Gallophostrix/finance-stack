# etl/common/adapters/bitcoin.py
from __future__ import annotations
from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

from etl.utils.http import HttpClient
from etl.utils.logging import setup_json_logging


# ------------- BTC -------------
CATEGORY = "tokens"
SUBCATEGORY = "Wallet"
SYMBOL = "BTC"
COINGECKO_ID = "bitcoin"
DEFAULT_BASE = "https://api.blockcypher.com/v1/btc/main"
DECIMALS = 8

# ------------- PRIVATE HELPERS -------------
def _sats_to_btc(sats: int) -> Decimal:
    """Convert satoshis to BTC."""
    return (Decimal(sats) / Decimal("1e8"))

def _parse_iso_utc_to_date(s: str) -> date:
    '''
    "2014-05-22T03:46:25Z" -> date
    '''
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()

def _get_addrs(cfg_chain: dict) -> List[str]:
    """
    Fetch all the BTC addresses from the YAML and cull them.
    """
    # Direct culling
    addrs = [a.strip() for a in (cfg_chain.get("addresses") or []) if a and a.strip()]
    return list(dict.fromkeys(addrs))

def _addr_payload(http: HttpClient, base_url: str, addr: str, token: Optional[str], min_confs: Optional[int]) -> Dict[str, Any]:
    """
    Query the BlockCypher API for a given Bitcoin address.

    Args:
        http:        Shared HttpClient (handles timeout, retries, rate-limit, logging)
        base_url:    Base API URL (e.g. "https://api.blockcypher.com/v1/btc/main")
        addr:        Bitcoin address (bech32 or legacy)
        token:       Optional BlockCypher API token
        min_confs:   Optional minimum number of confirmations to include

    Returns:
        Decoded JSON payload (Python dict) from the API response.

    Raises:
        ValueError: If the address is empty.
        HttpError:  If the HTTP request fails or returns a non-200 status code.
    """
    addr = (addr or "").strip()
    if not addr:
        if hasattr(http, "log") and http.log:
            http.log.error(
                "empty_address",
                extra={
                    "job": "etl-adapter",
                    "step": "fetch_balances",
                    "asset": SYMBOL
                }
            )
        raise ValueError("BTC address is empty")
    
    params: Dict[str, Any] = {}
    if token:
        params["token"] = token
    if min_confs is not None:
        params["confirmations"] = int(min_confs)
    
    url = f"{base_url.rstrip('/')}/addrs/{addr}"

    try:
        return http.get_json(url, params=params)
    except Exception as e:
        if hasattr(http, "log") and http.log:
            http.log.exception(
                "btc_addr_payload_error",
                extra={
                    "job":"etl-adapter",
                    "step":"fetch_addr_payload",
                    "asset":SYMBOL,
                    "addr_tail": addr[-6:]
                }
            )
        raise


# ------------- PUBLIC API -------------
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

    base_url = ((cfg_chain.get("provider", {}) or {}).get("base_url")
            or DEFAULT_BASE).rstrip("/")
    token = (cfg_chain.get("provider", {}) or {}).get("token") or None
    min_confs = (cfg_chain.get("provider", {}) or {}).get("min_confs")
    addrs = _get_addrs(cfg_chain)

    httpc = http or HttpClient(logger=log)
    total_sats = 0
    for addr in addrs:
        payload = _addr_payload(httpc, base_url, addr, token, min_confs)
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

    qty_btc = _sats_to_btc(total_sats)
    balances = {SYMBOL: qty_btc}
    pricing = cfg_chain.get("assets", [{}])[0].get("pricing", "auto")
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

def detect_flows(cfg_chain: dict, start_date: date, end_date: date, *,
                 logger=None, http: Optional[HttpClient]=None, page_limit: int = 50) -> List[dict]:
    """
    Renvoie une liste de flux:
      { "d": date, "category": CATEGORY, "subcategory": SUBCATEGORY, "asset": SYMBOL,
        "amount_native": Decimal, "kind": "in"|"out", "tx_hash": str|None }
    NOTE: on lit 1 page max par adresse; si la plus ancienne tx de la page > start_date,
          on loggue un warning (risque de troncature) mais on n’échoue pas.
    """
    log = logger or setup_json_logging()

    base_url = (cfg_chain.get("provider", {}) or {}).get("base_url") or "https://api.blockcypher.com/v1/btc/main"
    token = (cfg_chain.get("provider", {}) or {}).get("token") or None
    min_confs = (cfg_chain.get("provider", {}) or {}).get("min_confs")
    addrs = _get_addrs(cfg_chain)

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
            oldest = _parse_iso_utc_to_date(txrefs[-1].get("confirmed")) if txrefs[-1].get("confirmed") else None
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
            d = _parse_iso_utc_to_date(tx["confirmed"])
            if d < start_date:
                # Oldest tx reached, stop processing further (txrefs are in descending order)
                break
            if d > end_date:
                continue

            amt = _sats_to_btc(int(tx.get("value", 0)))
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
                    "addr_tail": addr[-6:],
                    "rows": len(flows)
                })

    if http is None:
        httpc.close()
    return flows