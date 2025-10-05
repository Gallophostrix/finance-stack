# etl/common/adapters/cardano/balances.py
from __future__ import annotations
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

def fetch_balances(
    cfg_chain: dict, *,
    logger=None,
    http: Optional[HttpClient] = None
) -> Tuple[Dict[str, Decimal], Dict[str, dict]]:
    """
    Fetches balances and metadata for ADA and CNTs from Koios.

    Args:
        cfg_chain: Parsed YAML configuration for the Cardano chain
        logger:    Optional logger (if None, a default JSON logger is created)
        http:      Optional shared HttpClient (if None, a new one is created and closed at the end)

    Returns:
        balances = {'ADA': <qty>, 'INDY': <qty>, 'iUSD': <qty>, ...}
        meta     = {symbol: {'pricing':..., 'coingecko_id':..., ...}}
            Metadata to help the orchestrator fetch market prices.
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
                        "asset":"ADA"
                    }
                )
        raise ValueError("Cardano stake key is empty")

    httpc = http or HttpClient(logger=log)
    koios = KoiosClient(httpc, base_url)
    wl = whitelist_from_cfg(cfg_chain)

    balances: Dict[str, Decimal] = {}
    meta: Dict[str, dict] = {}

    # ---------- ADA through account_info ----------
    qty_ada = Decimal(0)
    for sk in stake_keys:
        try:
            resp = koios.post(
                "account_info",
                {"_stake_addresses": [sk]},
                ctx={"stake_tail": sk[-6:]},
            ) or []
        except Exception:
            if http is None:
                httpc.close()
            raise  # fast fail for stake_key issues

        if isinstance(resp, list) and resp:
            lovelace = int(resp[0].get("total_balance") or 0)
            qty_ada += lovelace_to_ada(lovelace)
            log.info(
                "cardano_ada_balance",
                extra={
                    "job": "etl-api",
                    "step": "balances",
                    "asset": "ADA",
                    "stake_tail": sk[-6:],
                    "lovelace": lovelace,
                },
            )
        else:
            log.warning(
                "cardano_account_info_unexpected_shape",
                extra={
                    "job": "etl-api",
                    "step": "balances",
                    "asset": "ADA",
                    "stake_tail": sk[-6:],
                    "type": type(resp).__name__
                },
            )

    balances["ADA"] = qty_ada
    ADA_pricing = wl.get("ADA", {}).get("pricing", "auto")
    meta["ADA"] = {
        "pricing": ADA_pricing,
        "coingecko_id": wl.get("ADA", {}).get("coingecko_id", "cardano"),
        "decimals": ADA_DECIMALS,
    }

    # ---------- CNT through account_assets ----------
    try:
        assets_resp = koios.post(
            "account_assets",
            {"_stake_addresses": stake_keys},
            ctx={"stake_count": len(stake_keys)}
        ) or []
    except Exception:
        if http is None:
            httpc.close()
        raise

    if not isinstance(assets_resp, list):
        log.warning("cardano_account_assets_unexpected_shape",
                    extra={
                        "job":"etl-api",
                        "step":"balances",
                        "type": type(assets_resp).__name__,
                        "stake_count": len(stake_keys)
                        }
                    )
        assets_resp = []

    for entry in assets_resp:
        policy = (entry.get("policy_id") or "")  # string
        raw_name = (entry.get("asset_name") or "")  # hex string
        qty_raw = int(entry.get("quantity") or 0)

        sym, dec = resolve_cnt_symbol(policy, raw_name, wl)
        if not sym:
            # Warning if the CNT is not whitelisted
            log.warning(
                "cardano_cnt_ignored",
                extra={
                    "job": "etl-api",
                    "step": "balances",
                    "policy": policy,
                    "asset_tail": raw_name,
                    "qty_raw": qty_raw
                }
            )
            continue

        denom = (Decimal(10) ** Decimal(dec)) if dec > 0 else Decimal(1)
        qty = Decimal(qty_raw) / denom
        # Safety: some APIs may duplicate lines, so we sum just in case
        balances[sym] = balances.get(sym, Decimal(0)) + qty
        meta[sym] = {
            "pricing": wl.get(sym, {}).get("pricing", "auto"),
            "coingecko_id": wl.get(sym, {}).get("coingecko_id", ""),
            "decimals": dec,
            "policy_id": wl.get(sym, {}).get("policy_id"),
        }

    log.info(
        "cardano_balances_done",
        extra={
            "job": "etl-api",
            "step": "aggregate",
            "assets_count": len(balances),
            "stake_count": len(stake_keys)
        },
    )

    if http is None:
        httpc.close()

    return balances, meta