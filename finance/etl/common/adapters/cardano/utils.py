# etl/common/adapters/cardano/utils.py
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone, date
from typing import Any, Dict, Iterable, List, Tuple, Optional
import json, binascii

ADA_DECIMALS = 6
ADA_KEY = ("ADA", None, None)

def asset_key_cnt(policy_id: str, asset_name_hex: str) -> Tuple[str, Optional[str], Optional[str]]:
    return ("CNT", policy_id or "", asset_name_hex or "")

def lovelace_to_ada(lovelace: int) -> Decimal:
    """Convert lovelaces to ADA."""
    return (Decimal(lovelace) / Decimal("1e6"))

def utc_date_from_unix(ts: int) -> date:
    '''"1758982398" -> date'''
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()

def norm_asset_list(x) -> List[Dict[str, Any]]:
    """Normalize input into a list of dicts."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s in ("", "[]"):
            return []
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return [x]

def batch(it: Iterable[Any], n: int) -> Iterable[List[Any]]:
    """Batch an iterable into lists of size n."""
    buf: List[Any] = []
    for x in it:
        buf.append(x)
        if len(buf) == n:
            yield buf; buf = []
    if buf:
        yield buf

def whitelist_from_cfg(cfg_chain: dict) -> Dict[str, dict]:
    """
    Build a whitelist mapping ASSET_CODE -> pricing/meta info
    from the chain YAML.
    """
    wl: Dict[str, dict] = {}
    for a in cfg_chain.get("assets", []) or []:
        code = (a.get("code") or "").upper()
        wl[code] = {
            "pricing": a.get("pricing", "auto"),
            "coingecko_id": a.get("coingecko_id", ""),
            "policy_id": a.get("policy_id") or None,
            "decimals": int(a.get("decimals", ADA_DECIMALS if code=="ADA" else 0)),
        }
    return wl

def decode_asset_name(asset_name_hex: str) -> str:
    """Decode asset_name hex into ASCII when possible."""
    if not asset_name_hex:
        return ""
    try:
        return binascii.unhexlify(asset_name_hex).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def resolve_cnt_symbol(policy_id: str, asset_name_hex: str, wl: Dict[str, dict]) -> Tuple[str, int]:
    """Resolve CNT asset symbol and decimals from whitelist."""
    decoded = decode_asset_name(asset_name_hex)
    # 1) Prioritise policy_id match
    for code, info in wl.items():
        if code == "ADA":
            continue
        pid = info.get("policy_id")
        if pid and pid == policy_id:
            return code, int(info.get("decimals", 0))
    # 2) fallback on decoded name match
    if decoded:
        for code, info in wl.items():
            if code == "ADA":
                continue
            if decoded.upper() == code.upper():
                return code, int(info.get("decimals", 0))
    return "", 0
