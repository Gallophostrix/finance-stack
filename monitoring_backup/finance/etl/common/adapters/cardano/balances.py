# etl/common/adapters/cardano/balances.py
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Tuple, Optional, List

from etl.utils.logging import setup_json_logging
from etl.utils.http import HttpClient
from .koios import KoiosClient
from .utils import (
    ADA_DECIMALS,
    lovelace_to_ada,
    whitelist_from_cfg,
    resolve_cnt_symbol,
)

def _fetch_ada_rows(
    koios: KoiosClient,
    stake_keys: List[str],
    log,
    wl: Dict[str, dict],
) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Batch-fetch ADA balances for all stake keys using 'account_info'.
    Returns (rows, meta_ada).
    """
    rows: List[dict] = []
    meta: Dict[str, dict] = {
        "ADA": {
            "pricing": wl.get("ADA", {}).get("pricing", "auto"),
            "coingecko_id": wl.get("ADA", {}).get("coingecko_id", "cardano"),
            "decimals": ADA_DECIMALS,
        }
    }

    info = koios.post(
        "account_info",
        {"_stake_addresses": stake_keys},
        ctx={"stake_count": len(stake_keys)}
    ) or []
    ada_by_stake: Dict[str, Decimal] = {}

    if isinstance(info, list):
        for entry in info:
            stake = (entry.get("stake_address") or "").strip()
            lovelace = int(entry.get("total_balance") or 0)
            if stake:
                ada_by_stake[stake] = lovelace_to_ada(lovelace)
                log.info(
                    "cardano_ada_balance",
                    extra={
                        "job":"etl-api",
                        "step":"balances",
                        "asset_code":"ADA",
                        "stake_tail": stake[-6:],
                        "lovelace": lovelace
                    }
                )
    else:
        log.warning(
            "cardano_account_info_unexpected_shape",
            extra={
                "job":"etl-api",
                "step":"balances",
                "type": type(info).__name__
            }
        )

    # Emit one ADA row per stake (0 allowed to overwrite stale values)
    for sk in stake_keys:
        rows.append({
            "account_type": "stake_key",
            "external_identifier": sk,
            "asset_code": "ADA",
            "amount_native": ada_by_stake.get(sk, Decimal(0)),
        })

    return rows, meta

def _fetch_cnt_rows(
    koios: KoiosClient,
    stake_keys: List[str],
    log,
    wl: Dict[str, dict],
) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Batch-fetch CNT balances for all stake keys using 'account_assets'.
    Returns (rows, meta_for_cnts).
    """
    rows: List[dict] = []
    meta: Dict[str, dict] = {}

    assets_resp = koios.post("account_assets", {"_stake_addresses": stake_keys}, ctx={"stake_count": len(stake_keys)}) or []

    # Aggregate raw quantities by (stake, policy_id, asset_name)
    by_stake_cnt: Dict[Tuple[str, str, str], int] = defaultdict(int)

    if isinstance(assets_resp, list):
        for entry in assets_resp:
            stake = (entry.get("stake_address") or "").strip()
            policy = (entry.get("policy_id") or "")
            raw_name = (entry.get("asset_name") or "")
            qty_raw = int(entry.get("quantity") or 0)
            if stake and (policy or raw_name) and qty_raw:
                by_stake_cnt[(stake, policy, raw_name)] += qty_raw
    else:
        log.warning(
            "cardano_account_assets_unexpected_shape",
            extra={
                "job":"etl-api",
                "step":"balances",
                "type": type(assets_resp).__name__
            }
        )

    # Resolve symbol/decimals via whitelist and emit rows per stake
    for (stake, policy, raw_name), qraw in by_stake_cnt.items():
        sym, dec = resolve_cnt_symbol(policy, raw_name, wl)
        if not sym:
            log.warning(
                "cardano_cnt_ignored",
                extra={
                    "job":"etl-api",
                    "step":"balances",
                    "policy": policy,
                    "asset_tail": raw_name,
                    "qty_raw": qraw,
                    "stake_tail": stake[-6:]
                }
            )
            continue

        denom = (Decimal(10) ** Decimal(dec)) if dec > 0 else Decimal(1)
        qty = Decimal(qraw) / denom

        rows.append({
            "account_type": "stake_key",
            "external_identifier": stake,
            "asset_code": sym,
            "amount_native": qty,
        })

        if sym not in meta:
            meta[sym] = {
                "pricing": wl.get(sym, {}).get("pricing", "auto"),
                "coingecko_id": wl.get(sym, {}).get("coingecko_id", ""),
                "decimals": dec,
                "policy_id": wl.get(sym, {}).get("policy_id"),
            }

    return rows, meta

def fetch_balances(
    cfg_chain: dict,
    *,
    logger=None,
    http: Optional[HttpClient] = None
) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Fetch per-stake-key balances (ADA + CNT) from Koios, using batched endpoints.

    Args:
        cfg_chain: Parsed YAML configuration for the Cardano chain
        logger:    Optional logger (if None, a default JSON logger is created)
        http:      Optional shared HttpClient (if None, a new one is created and closed at the end)

    Returns:
        rows = [
          {"account_type":"stake_key","external_identifier": "<stake1...>","asset":"ADA","amount_native": Decimal},
          {"account_type":"stake_key","external_identifier": "<stake1...>","asset":"INDY","amount_native": Decimal},
          ...
        ]
        meta = {asset_code: {"pricing":..., "coingecko_id":..., "decimals": int, "policy_id": optional}}
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
        # Fast fail and log if stake_keys is empty or has empty entries
        log.error("empty_stake_key",
                    extra={
                        "job":"etl-api",
                        "step":"balances",
                        "asset_code":"ADA"
                    }
                )
        raise ValueError("Cardano stake key is empty")

    httpc = http or HttpClient(logger=log)
    koios = KoiosClient(httpc, base_url)
    wl = whitelist_from_cfg(cfg_chain)

    try:
        ada_rows, ada_meta = _fetch_ada_rows(koios, stake_keys, log, wl)
        cnt_rows, cnt_meta = _fetch_cnt_rows(koios, stake_keys, log, wl)

        rows = ada_rows + cnt_rows
        meta = {**ada_meta, **cnt_meta}

        log.info(
            "cardano_balances_done",
            extra={"job":"etl-api","step":"aggregate","assets_distinct": len(meta), "stake_count": len(stake_keys), "rows": len(rows)},
        )
        return rows, meta

    finally:
        if http is None:
            httpc.close()