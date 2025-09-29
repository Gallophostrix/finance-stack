# Return ADA and CNTs balances, meta-pricing and flows
import time
import requests
import json
from datetime import datetime, timezone, date
from typing import List, Dict, Optional, Tuple

# ------------- PARAMETERS -------------
RETRIES = 3
RETRY_DELAY_SEC = 1

# ------------- ADA + CNTs -------------
CATEGORY = "tokens"
SUBCATEGORY = "Wallet"
COINGECKO_ID = "cardano"
BASE_URL = "https://api.koios.rest/api/v1"

# ------------- PRIVATE HELPERS -------------

# BLOCKCHAIN HELPERS
def _lovelace_to_ada(lovelace: int) -> float:
    """Convert lovelaces to ADA."""
    return float(lovelace) / 1e6

def _asset_key_ada():
    return ("ADA", None, None)

def _asset_key_cnt(policy_id: str, asset_name_hex: str):
    return ("CNT", policy_id or "", asset_name_hex or "")

def _decode_asset_name(key, wl):
    """
    Decode asset_name hex into ASCII when possible.
    """
    sym, policy, hex_name = key

    # ADA: fixed
    if sym == "ADA":
        dec = wl.get("ADA", {}).get("decimals", 6)
        return "ADA", dec

    # CNT: map through whitelist
    decoded = bytes.fromhex(hex_name).decode("utf-8", errors="ignore") if hex_name else ""
    for s, info in wl.items():
        if s == "ADA":
            continue
        pid = info.get("policy_id")
        if pid and pid == policy:
            return s, (info.get("decimals") or 0)
        if (not pid) and decoded and decoded.upper() == s.upper():
            return s, (info.get("decimals") or 0)
    return "", 0

def _req_koios(method: str, endpoint: str, payload: dict, timeout=20):
    """
    Query the Koios API for a given Cardano stake key.

    Args:
    method: API request type
    endpoint: used API
    payload: JSON request file
        timeout: HTTP request timeout in seconds

    Returns:
        JSON result from Koios API
    """
    url = f"{BASE_URL}/{endpoint}"
    for _ in range(RETRIES):
        try:
            if method.upper() == "POST":
                r = requests.post(url, json=payload, timeout=timeout)
            elif method.upper() == "GET":
                r = requests.get(url, params=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, requests.HTTPError, ValueError, KeyError):
            time.sleep(RETRY_DELAY_SEC)
            continue
    raise RuntimeError(f"Koios request failed for {endpoint} [{method}]")

def _get_addresses_from_stk(stake_keys: List[str]) -> List[str]:
    """
    Fetch all the addresses associated to stake keys.
    """
    addrs: List[str] = []
    # Koios: account_addresses
    data = _req_koios("POST", "account_addresses", {"_stake_addresses": stake_keys})
    # data = [{"stake_address": ..., "addresses": ["addr1...", "addr1...", ...]}, ...]
    for entry in data or []:
        addrs.extend(entry.get("addresses", []) or [])
    # Culling
    addrs = [a.strip() for a in addrs if a and a.strip()]
    return list(dict.fromkeys(addrs))

# SCRIPT HELPERS

def _norm_asset_list(x):
    if x is None:
        return []
    # if Koios returns "[]"
    if isinstance(x, str):
        s = x.strip()
        if s == "[]" or s == "":
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            # if JSON invalid
            return []
    if isinstance(x, list):
        return x
    # If x already is a dict (should not happen)
    return [x]

def _unix_to_date_utc(ts: int) -> date:
    '''
    "1758982398" -> date
    '''
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()

def _batch(iterable, n):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf

def _account_txs(stake_keys: list[str]) -> list[dict]:
    """
    Aggregates TXs from multiple stake keys via GET /account_txs?_stake_address=...
    """
    out = []
    for sk in stake_keys:
        data = _req_koios("GET", "account_txs", {"_stake_address": sk}) or []
        # Only keep tx_hash and block_time
        for x in data:
            h = x.get("tx_hash"); ts = x.get("block_time")
            if h and ts is not None:
                out.append({"tx_hash": h, "block_time": ts})
    # Culling
    seen = set()
    dedup = []
    for x in out:
        if x["tx_hash"] in seen: 
            continue
        seen.add(x["tx_hash"])
        dedup.append(x)
    return dedup

def _whitelist(cfg_chain) -> Dict[str, dict]:
    wl = {}
    for a in cfg_chain.get("assets", []):
        sym = (a.get("symbol") or "").upper()
        wl[sym] = {
            "pricing": a.get("pricing", "auto"),
            "coingecko_id": a.get("coingecko_id", ""),
            "policy_id": a.get("policy_id", None),
        }
    return wl


# ------------- PUBLIC INTERFACE -------------
def fetch(cfg_chain):
    """
    Fetches balances and metadata for ADA and CNTs from Koios.

    Args:
        cfg_chain: Parsed YAML configuration for the Cardano chain.

    Returns:
        balances = {'ADA': <qty>, 'INDY': <qty>, 'iUSD': <qty>, ...}
            Always returned, even if balance = 0.0 (important to overwrite old values in DB).
        meta     = {symbol: {'pricing':'auto', 'coingecko_id':...}}
            Metadata to help the orchestrator fetch market prices.
    """

    balances = {}
    meta = {}

    stake_keys = cfg_chain.get("stake_keys", [])
    if not stake_keys:
        return balances, meta

    # Prepare whitelist from YAML config
    whitelist = _whitelist(cfg_chain)

    # 1) ADA balance via account_info
    ada_qty = 0.0
    for skey in stake_keys:
        try:
            data = _req_koios("POST", "account_info", {"_stake_addresses": [skey]})
            if data:
                lovelace = int(data[0].get("total_balance", "0"))
                ada_qty += _lovelace_to_ada(lovelace)
        except Exception:
            continue

    balances["ADA"] = ada_qty
    if "ADA" in whitelist:
        meta["ADA"] = {
            "pricing": whitelist["ADA"]["pricing"],
            "coingecko_id": whitelist["ADA"]["coingecko_id"],
            "decimals": 6,
        }

    # 2) Other assets (CNTs) via account_assets
    try:
        data = _req_koios("POST", "account_assets", {"_stake_addresses": stake_keys})
    except Exception:
        data = []

    for entry in data:
        raw_name = entry.get("asset_name", "")
        policy = entry.get("policy_id", "")
        sym = entry.get("asset_symbol", "")
        qty_raw = int(entry.get("quantity", "0"))
        decimals = int(entry.get("decimals", 0))
        qty = qty_raw / (10 ** decimals if decimals > 0 else 1)
        asset_key = _asset_key_cnt(policy, raw_name)

        # Decode asset name
        decoded_name = _decode_asset_name(asset_key, whitelist)[0]

        # Try to match against whitelist
        for sym, info in whitelist.items():
            if sym == "ADA":
                continue
            # Match by policy_id if provided, else by decoded name
            if info.get("policy_id") and info["policy_id"] == policy:
                balances[sym] = qty
                meta[sym] = {
                    "pricing": info.get("pricing"),
                    "coingecko_id": info.get("coingecko_id", ""),
                }
            elif decoded_name and decoded_name.upper() == sym.upper():
                balances[sym] = qty
                meta[sym] = {
                    "pricing": info.get("pricing"),
                    "coingecko_id": info.get("coingecko_id", ""),
                    "decimals": decimals,
                    "policy_id": info.get("policy_id"),
                }

    return balances, meta

def flows_detection(cfg_chain: dict, start_date: date, end_date: date) -> List[dict]:
    """
    Fetch gross flows of the month [start_date, end_date] for all the Cardano addresses of the whitelisted assets.
    The flow direction is given by the value of the tx (tx < 0 => out).
    """

    flows: List[dict] = []

    whitelist = _whitelist(cfg_chain)

    stake_keys = cfg_chain.get("stake_keys", [])
    if not stake_keys:
        return []

    try:
        my_addrs = _get_addresses_from_stk(stake_keys) or []
    except Exception:
        pass
    if not my_addrs:
        return []

    # List of all linked tx (POST /account_txs) (one quiery for all stake keys is enough)
    txs = _account_txs(stake_keys) or []

    # Date filtering
    txs_in_window = []
    for t in txs:
        ts = t.get("block_time")
        h  = t.get("tx_hash")
        if ts is None or not h:
            continue
        d = _unix_to_date_utc(int(ts))
        if start_date <= d <= end_date:
            txs_in_window.append({
                "tx_hash": h,
                "d": d
            })

    if not txs_in_window:
        return []

    # tx_info per batch
    for chunk in _batch([x["tx_hash"] for x in txs_in_window], 50):
        info = _req_koios("POST", "tx_info", {
            "_tx_hashes": chunk,
            "_inputs": True,
            "_assets": True,
            "_withdrawals": False,
            "_certs": False,
            "_metadata": False,
            "_scripts": False,
            "_bytecode": False
        }) or []

        by_hash = {t.get("tx_hash"): t for t in info}

        dict_date = {x["tx_hash"]: x["d"] for x in txs_in_window}

        for txh in chunk:

            # Extract date from txs_in_window
            d = dict_date.get(txh)
            if d is None:
                continue

            t_data = by_hash.get(txh)
            if not t_data:
                continue

            # Temporary storage for net ADA and CNTs per tx
            net_native: dict[tuple, int] = {}

            for i in t_data.get("inputs", []) or []:
                addr = (i.get("payment_addr") or {}).get("bech32")
                if addr not in my_addrs:
                    continue

                # --------- ADA ---------
                val = int(i.get("value") or 0)
                if val:
                    net_native[_asset_key_ada()] = net_native.get(_asset_key_ada(), 0) - val

                # --------- CNTs ---------
                al = _norm_asset_list(i.get("asset_list", None))

                for a in al:
                    k = _asset_key_cnt(a.get("policy_id"), a.get("asset_name",""))
                    qty = int(a.get("quantity") or 0)
                    if qty:
                        net_native[k] = net_native.get(k, 0) - qty

            for o in t_data.get("outputs", []) or []:
                addr = (o.get("payment_addr") or {}).get("bech32")
                if addr not in my_addrs:
                    continue

                # --------- ADA ---------
                val = int(o.get("value") or 0)
                if val:
                    net_native[_asset_key_ada()] = net_native.get(_asset_key_ada(), 0) + val

                # --------- CNTs ---------
                for a in _norm_asset_list(o.get("asset_list")):
                    k = _asset_key_cnt(a.get("policy_id",""), a.get("asset_name",""))
                    qty = int(a.get("quantity") or 0)
                    if qty:
                        net_native[k] = net_native.get(k, 0) + qty

            ada_net = net_native.get(_asset_key_ada(), 0)
            if ada_net:
                flows.append({
                    "d": d,
                    "category": "tokens",
                    "subcategory": cfg_chain.get("subcategory", "Wallet"),
                    "asset": "ADA",
                    "amount_native": round(abs(_lovelace_to_ada(ada_net)), 6),
                    "kind": "in" if ada_net > 0 else "out",
                    "tx_hash": txh,
                })

            for key, net_q in net_native.items():
                if key[0] == "ADA":
                    continue
                if net_q == 0:
                    continue
                sym, dec = _decode_asset_name(key, whitelist)
                if not sym:
                    continue

                qty_native = net_q / (10 ** dec if dec > 0 else 1)

                flows.append({
                    "d": d,
                    "category": "tokens",
                    "subcategory": cfg_chain.get("subcategory", "Wallet"),
                    "asset": sym,
                    "amount_native": round(abs(qty_native), 6),
                    "kind": "in" if net_q > 0 else "out",
                    "tx_hash": txh,
                })

    return flows
