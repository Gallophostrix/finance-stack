# etl/common/loaders/dims/parsers.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, List
import yaml # type: ignore

from .normalization import (
    normalize_provider_type, normalize_provider_name,
    normalize_account_type, normalize_group,
    normalize_asset_code, normalize_asset_class,
    DEFAULT_ASSET_BY_PROVIDER, pick_decimals,
)

# ----- Data specs -----
@dataclass
class ProviderSpec:
    provider_type: str
    provider_name: str

@dataclass
class AssetSpec:
    asset_code: str
    asset_class: str
    decimals: int | None = None
    coingecko_id: str | None = None
    is_active: bool = True

@dataclass
class AccountSpec:
    account_type: str
    external_identifier: str
    group: str
    label: str | None = None
    is_active: bool = True

# ----- YAML loader -----
def load_yaml(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"Top-level YAML must be a mapping: {path}")
    return data

# ----- Parsers -----
def parse_provider(doc: Mapping[str, Any]) -> ProviderSpec:
    p = doc.get("provider") or {}
    if not isinstance(p, Mapping):
        raise ValueError("Missing or invalid 'provider' mapping")
    t = normalize_provider_type(p.get("type", ""))
    n = normalize_provider_name(p.get("name", ""))
    return ProviderSpec(provider_type=t, provider_name=n)

def parse_assets(doc: Mapping[str, Any], provider: ProviderSpec) -> List[AssetSpec]:
    assets_raw = doc.get("assets")
    out: List[AssetSpec] = []

    if assets_raw is None:
        # For blockchains, deduce a single asset (BTC/ADA) if known
        if provider.provider_type == "blockchain":
            ded = DEFAULT_ASSET_BY_PROVIDER.get(provider.provider_name)
            if ded:
                out.append(AssetSpec(asset_code=normalize_asset_code(ded), asset_class="crypto"))
        return out

    if not isinstance(assets_raw, list):
        raise ValueError("'assets' must be a list of mappings")

    for idx, a in enumerate(assets_raw):
        if not isinstance(a, Mapping):
            raise ValueError(f"'assets[{idx}]' must be a mapping")
        code = normalize_asset_code(str(a.get("code", "")))
        cls = normalize_asset_class(str(a.get("class", "")))
        dec = a.get("decimals")
        decimals = None
        if dec is not None:
            try:
                decimals = int(dec)
                if decimals < 0:
                    raise ValueError
            except Exception:
                raise ValueError(f"'assets[{idx}].decimals' must be a non-negative integer")
        cg = a.get("coingecko_id")
        coingecko_id = str(cg).strip() or None if cg is not None else None
        is_active = bool(a.get("is_active", True))
        out.append(AssetSpec(asset_code=code, asset_class=cls, decimals=decimals, coingecko_id=coingecko_id, is_active=is_active))
    
    codes = [a.asset_code for a in out]
    dups = {c for c in codes if codes.count(c) > 1}
    if dups:
        raise ValueError(f"Duplicate asset codes in YAML: {sorted(dups)}")
    
    return out

def parse_accounts(doc: Mapping[str, Any]) -> List[AccountSpec]:
    accs_raw = doc.get("accounts")
    if accs_raw is None:
        return []
    if not isinstance(accs_raw, list):
        raise ValueError("'accounts' must be a list of mappings")
    out: List[AccountSpec] = []
    for idx, a in enumerate(accs_raw):
        if not isinstance(a, Mapping):
            raise ValueError(f"'accounts[{idx}]' must be a mapping")
        t = normalize_account_type(str(a.get("type", "")))
        ext_id = str(a.get("id", "")).strip()
        if not ext_id:
            raise ValueError(f"'accounts[{idx}].id' is required")
        g = str(a.get("group", "Wallet" if t in {"address","stake_key"} else "Bank"))
        group = normalize_group(g)
        label_raw = a.get("label")
        label = str(label_raw).strip() or None if label_raw is not None else None
        is_active = bool(a.get("is_active", True))
        out.append(AccountSpec(account_type=t, external_identifier=ext_id, group=group, label=label, is_active=is_active))
    
    keys = [(a.account_type, a.external_identifier) for a in out]
    dups = {k for k in keys if keys.count(k) > 1}
    if dups:
        raise ValueError(f"Duplicate accounts (type,id) in YAML: {sorted(dups)}")
    
    return out

# Convenience used by SQL layer (decimals resolution)
def finalize_asset_decimals(spec: AssetSpec) -> int:
    return pick_decimals(spec.asset_code, spec.asset_class, spec.decimals)
