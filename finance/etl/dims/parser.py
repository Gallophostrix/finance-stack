"""
Parse and validate asset YAML files into typed dataclasses.
No DB interaction here — pure parsing.

Expected YAML structure:
  provider:
    type: blockchain | bank | broker | cex | other
    name: str
  assets:
    - code: str
      class: crypto | actions | epargne | immo
      decimals: int
      coingecko_id: str (optional)
      is_active: bool
  accounts:
    - type: address | stake_key | iban | broker_account | other
      id: str
      group: wallet | CEX | PEA | AV | Bank
      label: str
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("root")

VALID_PROVIDER_TYPES = {"blockchain", "bank", "broker", "cex", "other"}
VALID_ASSET_CLASSES = {"crypto", "actions", "epargne", "immo"}
VALID_ACCOUNT_TYPES = {"address", "stake_key", "iban", "broker_account", "other"}
VALID_GROUPS = {"wallet", "CEX", "PEA", "AV", "Bank"}


@dataclass
class ProviderSpec:
    type: str
    name: str


@dataclass
class AssetSpec:
    code: str
    asset_class: str
    decimals: int
    is_active: bool
    coingecko_id: Optional[str] = None


@dataclass
class AccountSpec:
    account_type: str
    external_id: str
    group: str
    label: str


@dataclass
class DimFile:
    """Parsed content of one asset YAML file."""

    source_file: Path
    provider: ProviderSpec
    assets: list[AssetSpec]
    accounts: list[AccountSpec]


def _normalize_provider_name(name: str) -> str:
    """Normalize provider name: underscore → hyphen, lowercase."""
    return name.lower().replace("_", "-")


def _parse_provider(raw: dict, source: Path) -> Optional[ProviderSpec]:
    ptype = raw.get("type", "").strip()
    pname = raw.get("name", "").strip()

    if not ptype or not pname:
        log.error("provider_missing_fields", extra={"file": str(source), "raw": raw})
        return None

    if ptype not in VALID_PROVIDER_TYPES:
        log.error(
            "provider_invalid_type",
            extra={
                "file": str(source),
                "type": ptype,
                "valid": sorted(VALID_PROVIDER_TYPES),
            },
        )
        return None

    return ProviderSpec(type=ptype, name=_normalize_provider_name(pname))


def _parse_assets(raw_list: list, source: Path) -> list[AssetSpec]:
    result = []
    for i, raw in enumerate(raw_list):
        code = str(raw.get("code", "")).strip().upper()
        aclass = str(raw.get("class", "")).strip()
        decimals = raw.get("decimals", 0)
        active = raw.get("is_active", True)
        cg_id = raw.get("coingecko_id") or None

        errors = []
        if not code:
            errors.append("missing code")
        if aclass not in VALID_ASSET_CLASSES:
            errors.append(
                f"invalid class '{aclass}' (valid: {sorted(VALID_ASSET_CLASSES)})"
            )
        if not isinstance(decimals, int) or decimals < 0:
            errors.append(f"invalid decimals '{decimals}'")

        if errors:
            log.error(
                "asset_parse_error",
                extra={"file": str(source), "index": i, "errors": errors, "raw": raw},
            )
            continue

        result.append(
            AssetSpec(
                code=code,
                asset_class=aclass,
                decimals=int(decimals),
                is_active=bool(active),
                coingecko_id=cg_id,
            )
        )
    return result


def _parse_accounts(raw_list: list, source: Path) -> list[AccountSpec]:
    result = []
    for i, raw in enumerate(raw_list):
        atype = str(raw.get("type", "")).strip()
        ext_id = str(raw.get("id", "")).strip()
        group = str(raw.get("group", "")).strip()
        label = str(raw.get("label", "")).strip()

        errors = []
        if atype not in VALID_ACCOUNT_TYPES:
            errors.append(
                f"invalid type '{atype}' (valid: {sorted(VALID_ACCOUNT_TYPES)})"
            )
        if not ext_id:
            errors.append("missing id")
        if group not in VALID_GROUPS:
            errors.append(f"invalid group '{group}' (valid: {sorted(VALID_GROUPS)})")
        if not label:
            errors.append("missing label")

        if errors:
            log.error(
                "account_parse_error",
                extra={"file": str(source), "index": i, "errors": errors, "raw": raw},
            )
            continue

        result.append(
            AccountSpec(
                account_type=atype,
                external_id=ext_id,
                group=group,
                label=label,
            )
        )
    return result


def parse_file(path: Path) -> Optional[DimFile]:
    """
    Parse a single YAML file.
    Returns None if the file is invalid (errors are logged).
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("yaml_read_error", extra={"file": str(path), "error": str(e)})
        return None

    if not isinstance(raw, dict):
        log.error("yaml_invalid_structure", extra={"file": str(path)})
        return None

    provider = _parse_provider(raw.get("provider", {}), path)
    if provider is None:
        return None

    assets = _parse_assets(raw.get("assets", []), path)
    accounts = _parse_accounts(raw.get("accounts", []), path)

    if not assets and not accounts:
        log.warning("yaml_empty", extra={"file": str(path)})

    log.info(
        "yaml_parsed",
        extra={
            "file": path.name,
            "provider": provider.name,
            "assets": len(assets),
            "accounts": len(accounts),
        },
    )

    return DimFile(
        source_file=path,
        provider=provider,
        assets=assets,
        accounts=accounts,
    )


def parse_dir(assets_dir: Path) -> list[DimFile]:
    """
    Parse all *.yml files in assets_dir.
    Aborts if directory doesn't exist or is empty.
    """
    if not assets_dir.exists():
        raise FileNotFoundError(f"Assets directory not found: {assets_dir}")

    files = sorted(assets_dir.glob("*.yml"))
    if not files:
        raise ValueError(f"No YAML files found in {assets_dir}")

    log.info("yaml_scan", extra={"dir": str(assets_dir), "files": len(files)})

    results = []
    for f in files:
        dim = parse_file(f)
        if dim is not None:
            results.append(dim)

    if not results:
        raise ValueError(f"All YAML files in {assets_dir} failed to parse")

    log.info(
        "yaml_scan_done",
        extra={"parsed": len(results), "failed": len(files) - len(results)},
    )
    return results
