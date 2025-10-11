# etl/common/loaders/validation.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, List, Tuple, Optional, Any, Dict, Set

from etl.utils.dates import today_utc_date


ALLOWED_KINDS: Set[str] = {"in", "out", "fee", "interest"}


@dataclass(frozen=True)
class RowError:
    """Represents a validation error for a given row index and reason."""
    index: int
    reason: str


# ---------- Small helpers (pure) ----------

def _is_positive_int(x: Any) -> bool:
    try:
        return isinstance(x, int) and x > 0
    except Exception:
        return False


def _is_nonneg_decimal(x: Any) -> bool:
    return isinstance(x, Decimal) and x >= Decimal(0)


def _is_pos_decimal(x: Any) -> bool:
    return isinstance(x, Decimal) and x > Decimal(0)


def _norm_asset_code(s: Any) -> str:
    """Upper snake-ish: keep as-is except uppercasing. Do not mutate the tuple; return the normalized string."""
    try:
        return str(s).strip().upper()
    except Exception:
        return ""


def _default_max_date(d: Optional[date]) -> date:
    return d if isinstance(d, date) else today_utc_date()


# ---------- Balances validation ----------

def validate_balances_rows(
    rows: Iterable[Tuple[date, int, str, Decimal]],
    *,
    allow_zero: bool = True,
    max_date: Optional[date] = None,
) -> Tuple[List[Tuple[date, int, str, Decimal]], List[RowError]]:
    """
    Validate balances tuples for core.balances_native:
      (d, account_id, asset, amount_native)

    Rules:
      - d <= max_date (default: today UTC)
      - account_id > 0 (int)
      - asset non-empty (uppercased)
      - amount_native >= 0 (unless allow_zero=False -> >0)

    Returns:
      (valid_rows, errors) where valid_rows have asset uppercased.
    """
    valid: List[Tuple[date, int, str, Decimal]] = []
    errors: List[RowError] = []

    cutoff = _default_max_date(max_date)

    for i, (d, account_id, asset, amount) in enumerate(rows):
        # Date
        if not isinstance(d, date):
            errors.append(RowError(i, "d: not a date"))
            continue
        if d > cutoff:
            errors.append(RowError(i, f"d: future date ({d} > {cutoff})"))
            continue

        # account_id
        if not _is_positive_int(account_id):
            errors.append(RowError(i, "account_id: must be positive int"))
            continue

        # asset
        asset_norm = _norm_asset_code(asset)
        if not asset_norm:
            errors.append(RowError(i, "asset: empty/invalid"))
            continue

        # amount
        if allow_zero:
            if not _is_nonneg_decimal(amount):
                errors.append(RowError(i, "amount_native: must be >= 0 Decimal"))
                continue
        else:
            if not _is_pos_decimal(amount):
                errors.append(RowError(i, "amount_native: must be > 0 Decimal"))
                continue

        valid.append((d, account_id, asset_norm, amount))

    return valid, errors


# ---------- Flows validation ----------

def validate_flows_rows(
    rows: Iterable[Tuple[str, date, int, str, Decimal, str, Optional[str]]],
    *,
    allow_zero: bool = False,
    max_date: Optional[date] = None,
    deduplicate: bool = True,
) -> Tuple[List[Tuple[str, date, int, str, Decimal, str, Optional[str]]], List[RowError]]:
    """
    Validate flow tuples for core.flows_native:
      (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)

    Rules:
      - flow_uid non-empty (str)
      - d <= max_date (default: today UTC)
      - account_id > 0 (int)
      - asset non-empty (uppercased)
      - amount_native > 0 (unless allow_zero=True -> >= 0)
      - kind ∈ {'in','out','fee','interest'}

    Options:
      - deduplicate=True keeps the last occurrence per flow_uid.

    Returns:
      (valid_rows, errors) where valid_rows have asset uppercased.
    """
    cutoff = _default_max_date(max_date)

    tmp_valid: List[Tuple[str, date, int, str, Decimal, str, Optional[str]]] = []
    errors: List[RowError] = []

    for i, (uid, d, account_id, asset, amount, kind, origin_ref) in enumerate(rows):
        # uid
        if not isinstance(uid, str) or not uid.strip():
            errors.append(RowError(i, "flow_uid: empty/invalid"))
            continue

        # date
        if not isinstance(d, date):
            errors.append(RowError(i, "d: not a date"))
            continue
        if d > cutoff:
            errors.append(RowError(i, f"d: future date ({d} > {cutoff})"))
            continue

        # account_id
        if not _is_positive_int(account_id):
            errors.append(RowError(i, "account_id: must be positive int"))
            continue

        # asset
        asset_norm = _norm_asset_code(asset)
        if not asset_norm:
            errors.append(RowError(i, "asset: empty/invalid"))
            continue

        # amount
        if allow_zero:
            if not _is_nonneg_decimal(amount):
                errors.append(RowError(i, "amount_native: must be >= 0 Decimal"))
                continue
        else:
            if not _is_pos_decimal(amount):
                errors.append(RowError(i, "amount_native: must be > 0 Decimal"))
                continue

        # kind
        if kind not in ALLOWED_KINDS:
            errors.append(RowError(i, f"kind: invalid ({kind!r})"))
            continue

        tmp_valid.append((uid, d, account_id, asset_norm, amount, kind, origin_ref))

    if not deduplicate:
        return tmp_valid, errors

    # Deduplicate by flow_uid, keep the LAST occurrence (right-most wins)
    seen: Dict[str, Tuple[str, date, int, str, Decimal, str, Optional[str]]] = {}
    for row in tmp_valid:
        seen[row[0]] = row  # overwrite previous with same uid

    return list(seen.values()), errors
