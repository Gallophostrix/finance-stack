# etl/common/adapters/bitcoin/utils.py
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, date
from typing import List

CATEGORY = "tokens"
SUBCATEGORY = "Wallet"
SYMBOL = "BTC"
COINGECKO_ID = "bitcoin"
DEFAULT_BASE = "https://api.blockcypher.com/v1/btc/main"
DECIMALS = 8

def sats_to_btc(sats: int) -> Decimal:
    """Convert satoshis to BTC."""
    return (Decimal(sats) / Decimal("1e8"))

def parse_iso_utc_to_date(s: str) -> date:
    '''
    "2014-05-22T03:46:25Z" -> date
    '''
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()

def get_addrs(cfg_chain: dict) -> List[str]:
    """
    Fetch all the BTC addresses from the YAML and cull them.
    """
    # Direct culling
    addrs = [a.strip() for a in (cfg_chain.get("addresses") or []) if a and a.strip()]
    return list(dict.fromkeys(addrs))
