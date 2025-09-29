# Return BTC balances, meta-pricing and flows
import time
import requests
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple


# ------------- PARAMETERS -------------
RETRIES = 3
RETRY_DELAY_SEC = 1

# ------------- BTC -------------
CATEGORY = "tokens"
SUBCATEGORY = "Wallet"
SYMBOL = "BTC"
COINGECKO_ID = "bitcoin"
BASE_URL = "https://api.blockcypher.com/v1/btc/main"
MAX_PAGE_LIMIT = 50     # Default tx number per page
MIN_CONFS = 6           # Confirmation number required

# ------------- PRIVATE HELPERS -------------
def _sats_to_btc(sats: int) -> float:
    """Convert satoshis to BTC."""
    return float(sats) / 1e8

def _parse_iso_utc_to_date(s: str) -> date:
    '''
    "2014-05-22T03:46:25Z" -> date
    '''
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()

def _get_addr_payload(addr: str, token: Optional[str], params: Optional[dict]=None, timeout=15):
    """
    Query the BlockCypher API for a given Bitcoin address.

    Args:
        base_url: Base API URL (default: https://api.blockcypher.com/v1/btc/main)
        addr: Bitcoin address (string)
        token: Optional BlockCypher API token (string)
        params: OPtional additional parameters
        timeout: HTTP request timeout in seconds

    Returns:
        JSON payload from BlockCypher API
    """
    p = dict(params or {})
    if token:
        p["token"] = token
    r = requests.get(f"{BASE_URL}/addrs/{addr}", params=p, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _get_addrs(cfg_chain: dict) -> List[str]:
    """
    Fetch all the BTC addresses from the YAML and cull them.
    """
    addrs: List[str] = []
    for a in cfg_chain.get("assets", []):
        if a.get("symbol", "").upper() == SYMBOL:
            addrs.extend(a.get("addresses", []))
    # Culling
    addrs = [x.strip() for x in addrs if x and x.strip()]
    return list(dict.fromkeys(addrs))

def _iter_txrefs(addr: str, token: Optional[str], limit: int = MAX_PAGE_LIMIT, min_confs: int =MIN_CONFS):
    """
    Returns at most 'limit' txrefs for the given address on one page (if more txs were made, they won't be included)
    """
    params = {"limit": limit}
    if min_confs is not None:
        params["confirmations"] = min_confs
    data = _get_addr_payload(addr, token, params=params)
    for txr in data.get("txrefs", []) or []:
        yield txr

def fetch(cfg_chain):
    """
    Fetch balances and metadata for BTC from BlockCypher.

    Args:
        cfg_chain: Parsed YAML configuration for the Bitcoin chain.

    Returns:
        balances = {"BTC": <qty_btc>}
            Always returned, even if balance = 0.0 (important to overwrite old values in DB).
        meta     = {"BTC": {"pricing":"auto", "coingecko_id":"bitcoin", "decimals":8}}
            Metadata to help the orchestrator fetch market prices.
    """
    # Token check
    token = (cfg_chain.get("provider", {}) or {}).get("token") or None
    use_final = bool((cfg_chain.get("provider", {}) or {}).get("use_final_balance", True))

    addrs = _get_addrs(cfg_chain)

    total_sats = 0
    for addr in addrs:
        for _ in range(RETRIES):
            try:
                data = _get_addr_payload(addr, token)
                sats = int(data["final_balance"] if use_final else data["balance"])
                total_sats += sats
                break
            except (requests.RequestException, ValueError, KeyError):
                time.sleep(RETRY_DELAY_SEC)
                continue

    qty_btc = _sats_to_btc(total_sats)

    # Always return balance + meta, even if balance is 0.0
    balances = {SYMBOL: qty_btc}
    meta = {SYMBOL: {"pricing": "auto", "coingecko_id": COINGECKO_ID, "decimals": 8}}
    return balances, meta

def flows_detection(cfg_chain: dict, start_date: date, end_date: date) -> List[dict]:
    """
    Fetch gross flows of the month [start_date, end_date] for all the BTC addresses.
    The flow direction is given by the sign of the tx (tx_input_n == -1 => output => inflow).
    """
    token = (cfg_chain.get("provider", {}) or {}).get("token") or None
    addrs = _get_addrs(cfg_chain)

    flows: List[dict] = []

    for addr in addrs:
        for tx in _iter_txrefs(addr, token):
            if not tx.get("confirmed"):
                continue
            d = _parse_iso_utc_to_date(tx["confirmed"])
            if d < start_date:
                break
            if d > end_date:
                continue

            sign = 1 if tx.get("tx_input_n", -1) == -1 else -1
            amt_btc = _sats_to_btc(int(tx["value"]))

            flows.append({
                "d": d,
                "category": CATEGORY,
                "subcategory": SUBCATEGORY,
                "asset": SYMBOL,
                "amount_native": round(amt_btc, 8),
                "kind": "in" if sign > 0 else "out",
                "tx_hash": tx.get("tx_hash"),
            })

    return flows
