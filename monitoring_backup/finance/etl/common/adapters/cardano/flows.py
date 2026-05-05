# etl/common/adapters/cardano/flows.py
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from etl.utils.http import HttpClient
from etl.utils.logging import setup_json_logging

from .koios import KoiosClient
from .utils import (
    batch,
    lovelace_to_ada,
    norm_asset_list,
    resolve_cnt_symbol,
    utc_date_from_unix,
    whitelist_from_cfg,
)


def _collect_candidates(
    koios: KoiosClient,
    stake_keys: List[str],
    start_date: date,
    end_date: date,
    log,
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """
    Phase 1: build address->stake map and list unique tx in [start_date, end_date].
    Returns:
      - addr_to_stake: {payment_address -> stake_key}
      - txs: [{"tx_hash": str, "d": date}, ...] (deduplicated across stakes)
    """
    # 1) addresses for all stake keys
    addr_resp = (
        koios.post(
            "account_addresses",
            {"_stake_addresses": stake_keys},
            ctx={"stake_count": len(stake_keys)},
        )
        or []
    )

    if not isinstance(addr_resp, list):
        log.warning(
            "cardano_account_addresses_unexpected_shape",
            extra={"job": "etl-api", "step": "flows", "type": type(addr_resp).__name__},
        )
        addr_resp = []

    addr_to_stake: Dict[str, str] = {}
    for row in addr_resp:
        stake = (row.get("stake_address") or "").strip()
        for a in row.get("addresses") or []:
            addr = (a or "").strip()
            if stake and addr:
                addr_to_stake[addr] = stake

    if not addr_to_stake:
        return {}, []

    # 2) tx list per stake, deduplicated, filtered by window
    seen: Set[str] = set()
    txs: List[Dict[str, Any]] = []

    for sk in stake_keys:
        r = (
            koios.get(
                "account_txs",
                params={"_stake_address": sk},
                ctx={"stake_tail": sk[-6:]},
            )
            or []
        )
        if not isinstance(r, list):
            log.warning(
                "cardano_account_txs_unexpected_shape",
                extra={
                    "job": "etl-api",
                    "step": "flows",
                    "type": type(r).__name__,
                    "stake_tail": sk[-6:],
                },
            )
            continue
        for x in r:
            h = x.get("tx_hash")
            ts = x.get("block_time")
            if not h or ts is None or h in seen:
                continue
            d = utc_date_from_unix(int(ts))
            if start_date <= d <= end_date:
                seen.add(h)
                txs.append({"tx_hash": h, "d": d})

    return addr_to_stake, txs


def _flows_from_txinfo(
    koios: KoiosClient,
    txs: List[Dict[str, Any]],
    addr_to_stake: Dict[str, str],
    wl: Dict[str, dict],
    txinfo_batch: int,
    log,
) -> List[dict]:
    """
    Phase 2: fetch tx_info in batches, compute net amounts per (stake, tx, asset),
    and emit normalized rows with positive amount_native and kind in/out.
    """
    if not txs:
        return []

    flows: List[dict] = []
    by_hash_date = {x["tx_hash"]: x["d"] for x in txs}

    for chunk in batch((x["tx_hash"] for x in txs), txinfo_batch):
        info = (
            koios.post(
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
                ctx={
                    "chunk_size": len(chunk),
                    "tx_tail": (chunk[0][-6:] if chunk else None),
                },
            )
            or []
        )

        if not isinstance(info, list):
            log.warning(
                "cardano_tx_info_unexpected_shape",
                extra={"job": "etl-api", "step": "flows", "type": type(info).__name__},
            )
            info = []

        # net[(stake, txh, "ADA", None, None)] = int(lovelace)
        # net[(stake, txh, "CNT", policy_id, asset_name_hex)] = int(qty_raw)
        net: Dict[Tuple[str, str, str, Optional[str], Optional[str]], int] = (
            defaultdict(int)
        )
        by_hash = {t.get("tx_hash"): t for t in info}

        for txh in chunk:
            t = by_hash.get(txh)
            d = by_hash_date.get(txh)
            if not t or not d:
                continue

            # --- Inputs: value leaving the wallet (negative) ---
            for i in t.get("inputs") or []:
                addr = ((i.get("payment_addr") or {}).get("bech32") or "").strip()
                stake = addr_to_stake.get(addr)
                if not stake:
                    continue
                val = int(i.get("value") or 0)
                if val:
                    net[(stake, txh, "ADA", None, None)] -= val
                for a in norm_asset_list(i.get("asset_list")):
                    pid = a.get("policy_id") or ""
                    an = a.get("asset_name") or ""
                    qty = int(a.get("quantity") or 0)
                    if qty:
                        net[(stake, txh, "CNT", pid, an)] -= qty

            # --- Outputs: value entering the wallet (positive) ---
            for o in t.get("outputs") or []:
                addr = ((o.get("payment_addr") or {}).get("bech32") or "").strip()
                stake = addr_to_stake.get(addr)
                if not stake:
                    continue
                val = int(o.get("value") or 0)
                if val:
                    net[(stake, txh, "ADA", None, None)] += val
                for a in norm_asset_list(o.get("asset_list")):
                    pid = a.get("policy_id") or ""
                    an = a.get("asset_name") or ""
                    qty = int(a.get("quantity") or 0)
                    if qty:
                        net[(stake, txh, "CNT", pid, an)] += qty

        # --- Emit one row per (stake, tx, asset) with abs(amount) and kind ---
        for (stake, txh, typ, pid, an), q in net.items():
            if q == 0:
                continue
            d = by_hash_date.get(txh)
            if not d:
                continue

            if typ == "ADA":
                flows.append(
                    {
                        "d": d,
                        "account_type": "stake_key",
                        "external_identifier": stake,
                        "asset": "ADA",
                        "amount_native": abs(lovelace_to_ada(q)),
                        "kind": "in" if q > 0 else "out",
                        "tx_hash": txh,
                    }
                )
            else:
                sym, dec = resolve_cnt_symbol(pid or "", an or "", wl)
                if not sym:
                    log.warning(
                        "cardano_cnt_flow_ignored",
                        extra={
                            "job": "etl-api",
                            "step": "flows",
                            "policy": pid,
                            "asset_tail": an,
                            "q_raw": q,
                            "stake_tail": stake[-6:],
                        },
                    )
                    continue
                denom = (Decimal(10) ** Decimal(dec)) if dec > 0 else Decimal(1)
                qty = Decimal(q) / denom
                flows.append(
                    {
                        "d": d,
                        "account_type": "stake_key",
                        "external_identifier": stake,
                        "asset": sym,
                        "amount_native": abs(qty),
                        "kind": "in" if q > 0 else "out",
                        "tx_hash": txh,
                    }
                )

        log.info(
            "cardano_flows_batch_done",
            extra={
                "job": "etl-api",
                "step": "flows",
                "batch_size": len(chunk),
                "flows_total": len(flows),
            },
        )

    return flows


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
            "account_type": "stake_key",
            "external_identifier": "<stake1...>",
            "asset": "<SYM>",                 # ADA or whitelisted CNT
            "amount_native": Decimal(>0),     # absolute value
            "kind": "in" | "out",
            "tx_hash": "<hash>",
          },
          ...
        ]
    """
    log = logger or setup_json_logging()

    base_url = (cfg_chain.get("source", {}) or {}).get("base_url")
    if base_url:
        base_url = base_url.rstrip("/")
    else:
        base_url = None

    stake_keys = [
        (acc.get("id") or "").strip()
        for acc in (cfg_chain.get("accounts") or [])
        if isinstance(acc, dict) and acc.get("type") == "stake_key"
    ]
    if not stake_keys or any(not sk for sk in stake_keys):
        log.error("empty_stake_key", extra={"job": "etl-api", "step": "flows"})
        raise ValueError("Cardano stake key is empty")

    httpc = http or HttpClient(logger=log)
    koios = KoiosClient(httpc, base_url)
    wl = whitelist_from_cfg(cfg_chain)

    try:
        # Phase 1: build address->stake map + candidate txs in window
        addr_to_stake, txs = _collect_candidates(
            koios, stake_keys, start_date, end_date, log
        )
        if not addr_to_stake or not txs:
            log.info(
                "cardano_flows_done",
                extra={"job": "etl-api", "step": "flows", "rows": 0},
            )
            return []

        # Phase 2: compute per-(stake, tx, asset) net and emit rows
        flows = _flows_from_txinfo(koios, txs, addr_to_stake, wl, txinfo_batch, log)

        log.info(
            "cardano_flows_done",
            extra={"job": "etl-api", "step": "flows", "rows": len(flows)},
        )
        return flows

    finally:
        if http is None:
            httpc.close()
