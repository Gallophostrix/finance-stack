# etl/common/adapters/cardano/flows.py
from __future__ import annotations
from decimal import Decimal
from datetime import date
from typing import Dict, List, Optional, Any, Set

from etl.utils.logging import setup_json_logging
from etl.utils.http import HttpClient
from .koios import KoiosClient
from .utils import (
    lovelace_to_ada,
    whitelist_from_cfg,
    resolve_cnt_symbol,
    utc_date_from_unix,
    norm_asset_list,
    batch,
)

def detect_flows(
    cfg_chain: dict,
    start_date: date,
    end_date: date,
    *,
    logger=None,
    http: Optional[HttpClient] = None,
    txinfo_batch: int = 50,
    ) -> List[dict]:
    """
    Returns net flows per transaction in the window [start_date, end_date].

    Args:
        cfg_chain: Parsed YAML configuration for the Cardano chain.
        start_date: Start date (inclusive).
        end_date:   End date (inclusive).
        logger:     Optional logger (if None, a default JSON logger is created).
        http:       Optional shared HttpClient (if None, a new one is created and closed at the end).
        txinfo_batch: Max number of tx_hashes per tx_info request (default 50).
    
    Returns:
        [
          {
            "d": date,
            "category": "tokens",
            "subcategory": <cfg_chain.subcategory|Wallet>,
            "asset": "<SYM>",                 # "ADA" or whitelisted CNT
            "amount_native": Decimal(>0),     # always positive
            "kind": "in" | "out",             # flow direction
            "tx_hash": "<hash>"
          },
          ...
        ]
    """
    log = logger or setup_json_logging()

    base_url = ((cfg_chain.get("provider", {}) or {}).get("base_url") or "").rstrip("/") or None
    raw = cfg_chain.get("stake_keys") or []
    stake_keys = [(sk or "").strip() for sk in raw]
    if not stake_keys or any(not sk for sk in stake_keys):
        log.error("empty_stake_key",
                  extra={
                      "job": "etl-api",
                      "step": "flows"
                      }
                )
        raise ValueError("Cardano stake key is empty")

    subcat = cfg_chain.get("subcategory", "Wallet")

    httpc = http or HttpClient(logger=log)
    koios = KoiosClient(httpc, base_url)
    wl = whitelist_from_cfg(cfg_chain)

    # 1) Fetch addresses from stake keys
    try:
        addr_resp = koios.post(
            "account_addresses",
            {"_stake_addresses": stake_keys},
            ctx={"stake_count": len(stake_keys)},
        ) or []
    except Exception:
        if http is None:
            httpc.close()
        raise
    
    if not isinstance(addr_resp, list):
        log.warning("cardano_tx_info_unexpected_shape",
                    extra={
                        "job":"etl-api",
                        "step":"flows",
                        "type": type(addr_resp).__name__
                        }
                    )
        addr_resp = []

    addrs: List[str] = []
    for row in addr_resp:
        addrs.extend((row.get("addresses") or []))
    addrs = [a.strip() for a in addrs if a and a.strip()]
    addr_set: Set[str] = set(addrs)
    if not addr_set:
        if http is None:
            httpc.close()
        return []

    # 2) Tx list for all stake keys, filter by date
    seen: Set[str] = set()
    txs: List[Dict[str, Any]] = []
    for sk in stake_keys:
        try:
            r = koios.get(
                "account_txs",
                params={"_stake_address": sk},
                ctx={"stake_tail": sk[-6:]},
            ) or []
        except Exception:
            if http is None:
                httpc.close()
            raise
        for x in r:
            h = x.get("tx_hash")
            ts = x.get("block_time")
            if not h or ts is None or h in seen:
                continue
            seen.add(h)
            d = utc_date_from_unix(int(ts))
            if start_date <= d <= end_date:
                txs.append({"tx_hash": h, "d": d})

    if not txs:
        if http is None:
            httpc.close()
        return []

    flows: List[dict] = []

    # 3) batching tx_info requests
    for chunk in batch((x["tx_hash"] for x in txs), txinfo_batch):
        try:
            info = koios.post(
                "tx_info",
                {
                    "_tx_hashes": list(chunk),
                    "_inputs": True,
                    "_assets": True,
                    "_withdrawals": False,
                    "_certs": False,
                    "_metadata": False,
                    "_scripts": False,
                    "_bytecode": False,
                },
                ctx={"chunk_size": len(chunk), "tx_tail": (chunk[0][-6:] if chunk else None)},
            ) or []
        except Exception:
            if http is None:
                httpc.close()
            raise

        if not isinstance(info, list):
            log.warning("cardano_tx_info_unexpected_shape",
                        extra={
                            "job":"etl-api",
                            "step":"flows",
                            "type": type(info).__name__
                            }
                        )
            info = []


        by_hash = {t.get("tx_hash"): t for t in info}
        date_by_hash = {x["tx_hash"]: x["d"] for x in txs}

        for txh in chunk:
            t = by_hash.get(txh)
            d = date_by_hash.get(txh)
            if not t or not d:
                continue

            # Net native amounts
            net: Dict[tuple, int] = {}

            # Inputs = value taken from the wallet
            for i in (t.get("inputs") or []):
                addr = ((i.get("payment_addr") or {}).get("bech32") or "").strip()
                if addr in addr_set:
                    val = int(i.get("value") or 0)
                    if val:
                        net[("ADA", None, None)] = net.get(("ADA", None, None), 0) - val
                    for a in norm_asset_list(i.get("asset_list")):
                        pid = (a.get("policy_id") or "")
                        an = (a.get("asset_name") or "")
                        qty = int(a.get("quantity") or 0)
                        if qty:
                            net[("CNT", pid, an)] = net.get(("CNT", pid, an), 0) - qty

            # Outputs = value sent to the wallet
            for o in (t.get("outputs") or []):
                addr = ((o.get("payment_addr") or {}).get("bech32") or "").strip()
                if addr in addr_set:
                    val = int(o.get("value") or 0)
                    if val:
                        net[("ADA", None, None)] = net.get(("ADA", None, None), 0) + val
                    for a in norm_asset_list(o.get("asset_list")):
                        pid = (a.get("policy_id") or "")
                        an = (a.get("asset_name") or "")
                        qty = int(a.get("quantity") or 0)
                        if qty:
                            net[("CNT", pid, an)] = net.get(("CNT", pid, an), 0) + qty

            # ADA
            ada_net = net.get(("ADA", None, None), 0)
            if ada_net:
                flows.append({
                    "d": d,
                    "category": "tokens",
                    "subcategory": subcat,
                    "asset": "ADA",
                    "amount_native": abs(lovelace_to_ada(ada_net)),
                    "kind": "in" if ada_net > 0 else "out",
                    "tx_hash": txh,
                })

            # CNT
            for (typ, pid, an), q in net.items():
                if typ == "ADA" or q == 0:
                    continue
                sym, dec = resolve_cnt_symbol(pid or "", an or "", wl)
                if not sym:
                    # If not whitelisted, ignore silently
                    log.warning(
                        "cardano_cnt_flow_ignored",
                        extra={
                            "job": "etl-api",
                            "step": "flows",
                            "policy": pid,
                            "asset_tail": an,
                            "q_raw": q,
                        },
                    )
                    continue
                denom = (Decimal(10) ** Decimal(dec)) if dec > 0 else Decimal(1)
                qty = Decimal(q) / denom
                flows.append({
                    "d": d,
                    "category": "tokens",
                    "subcategory": subcat,
                    "asset": sym,
                    "amount_native": abs(qty),
                    "kind": "in" if q > 0 else "out",
                    "tx_hash": txh,
                })

        log.info(
            "cardano_flows_batch_done",
            extra={
                "job": "etl-api",
                "step": "flows",
                "batch_size": len(chunk),
                "flows_total": len(flows),
            },
        )

    if http is None:
        httpc.close()

    return flows
