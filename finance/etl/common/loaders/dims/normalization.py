# etl/common/loaders/dims/normalization.py
from __future__ import annotations
from typing import Optional

# ----- Allowed vocab -----
ALLOWED_PROVIDER_TYPES = {"blockchain", "bank", "broker", "cex", "platform"}
ALLOWED_ACCOUNT_TYPES = {"address", "stake_key", "iban", "broker_account", "other"}
ALLOWED_GROUPS = {"Wallet", "CEX", "PEA", "AV", "Bank"}
ALLOWED_ASSET_CLASSES = {"crypto", "actions", "epargne"}

# ----- Defaults / heuristics -----
DEFAULT_ASSET_BY_PROVIDER = {
    "bitcoin": "BTC",
    "cardano": "ADA",
}
DEFAULT_DECIMALS_BY_ASSET = {
    "BTC": 8,
    "ADA": 6,
}
DEFAULT_DECIMALS_FALLBACK = 2

# ----- Normalizers -----
def normalize_provider_type(v: str) -> str:
    s = (v or "").strip()
    if s not in ALLOWED_PROVIDER_TYPES:
        raise ValueError(f"Invalid provider.type: {v!r}. Allowed: {sorted(ALLOWED_PROVIDER_TYPES)}")
    return s

def normalize_provider_name(v: str) -> str:
    """lower-kebab normalization: 'Amundi PEA' -> 'amundi-pea'."""
    s = (v or "").strip().lower().replace("_", "-")
    s = "-".join(part for part in s.split() if part)
    if not s:
        raise ValueError("provider.name is required")
    return s

def normalize_account_type(v: str) -> str:
    s = (v or "").strip()
    if s not in ALLOWED_ACCOUNT_TYPES:
        raise ValueError(f"Invalid account.type: {v!r}. Allowed: {sorted(ALLOWED_ACCOUNT_TYPES)}")
    return s

def normalize_group(v: str) -> str:
    s = (v or "").strip()
    if s not in ALLOWED_GROUPS:
        raise ValueError(f"Invalid group: {v!r}. Allowed: {sorted(ALLOWED_GROUPS)}")
    return s

def normalize_asset_code(v: str) -> str:
    s = (v or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not s:
        raise ValueError("asset.code is required")
    return s

def normalize_asset_class(v: str) -> str:
    s = (v or "").strip()
    if s not in ALLOWED_ASSET_CLASSES:
        raise ValueError(f"Invalid asset.class: {v!r}. Allowed: {sorted(ALLOWED_ASSET_CLASSES)}")
    return s

def pick_decimals(asset_code: str, asset_class: str, provided: Optional[int]) -> int:
    """Priority: provided → known-by-asset → fallback."""
    if isinstance(provided, int) and provided >= 0:
        return provided
    if asset_code in DEFAULT_DECIMALS_BY_ASSET:
        return DEFAULT_DECIMALS_BY_ASSET[asset_code]
    return DEFAULT_DECIMALS_FALLBACK
