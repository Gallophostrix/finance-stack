#!/usr/bin/env python3
"""
Import monthly balances from a CSV file into core.balances_native.

CSV format:
  account_label,asset,amount
  "Bank account and other cash accounts",COMPTE_COURANT,1500.00
  "Bourse Direct PEA account",WORLD,10000.00

Usage:
  python scripts/import_balances.py --date 2025-01-01 --file finance/data/balances/2025-01-01.csv
  python scripts/import_balances.py --date 2025-01-01 --file finance/data/balances/2025-01-01.csv --dry-run
"""

import argparse
import csv
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from etl.common.db import connect
from etl.common.logging import setup_logging

log = setup_logging()


# ---------- DB helpers ----------


def _load_account_map(conn) -> dict[str, int]:
    """label -> account_id for all active accounts."""
    with conn.cursor() as cur:
        cur.execute("SELECT account_id, label FROM core.accounts WHERE is_active")
        mapping = {label: aid for aid, label in cur.fetchall()}
    log.info("account_map_loaded", extra={"count": len(mapping)})
    return mapping


def _load_asset_set(conn) -> set[str]:
    """Set of active asset codes."""
    with conn.cursor() as cur:
        cur.execute("SELECT asset_code FROM core.assets WHERE is_active")
        assets = {r[0] for r in cur.fetchall()}
    log.info("asset_set_loaded", extra={"count": len(assets)})
    return assets


# ---------- CSV ----------


def _parse_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            rows.append(
                {
                    "line": i,
                    "account_label": row["account_label"].strip(),
                    "asset": row["asset"].strip(),
                    "amount": row["amount"].strip(),
                }
            )
    log.info("csv_parsed", extra={"file": str(path), "rows": len(rows)})
    return rows


# ---------- Validation ----------


def _validate(rows, account_map, asset_set) -> tuple[list, list]:
    valid, errors = [], []
    for r in rows:
        errs = []
        if r["account_label"] not in account_map:
            errs.append(f"unknown account_label '{r['account_label']}'")
        if r["asset"] not in asset_set:
            errs.append(f"unknown asset '{r['asset']}'")
        try:
            amount = Decimal(r["amount"])
            if amount < 0:
                errs.append("amount must be >= 0")
        except InvalidOperation:
            errs.append(f"invalid amount '{r['amount']}'")
            amount = None

        if errs:
            errors.append({"line": r["line"], "errors": errs})
        else:
            valid.append(
                {
                    "account_id": account_map[r["account_label"]],
                    "asset": r["asset"],
                    "amount": amount,
                }
            )
    return valid, errors


# ---------- DB write ----------


def _upsert(conn, cut_date: date, rows: list) -> int:
    sql = """
    INSERT INTO core.balances_native (d, account_id, asset, amount_native, observed_at)
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (d, account_id, asset) DO UPDATE
      SET amount_native = EXCLUDED.amount_native,
          observed_at   = EXCLUDED.observed_at
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [(cut_date, r["account_id"], r["asset"], r["amount"]) for r in rows],
        )
    conn.commit()
    return len(rows)


# ---------- Main ----------


def main() -> None:
    ap = argparse.ArgumentParser(description="Import monthly balances from CSV")
    ap.add_argument(
        "--date", required=True, help="Cut date YYYY-MM-DD (must be 1st of month)"
    )
    ap.add_argument("--file", required=True, help="Path to CSV file")
    ap.add_argument("--dry-run", action="store_true", help="Validate only, no DB write")
    args = ap.parse_args()

    try:
        cut_date = date.fromisoformat(args.date)
    except ValueError:
        log.error("invalid_date", extra={"date": args.date})
        sys.exit(1)

    if cut_date.day != 1:
        log.error("date_not_first_of_month", extra={"date": str(cut_date)})
        sys.exit(1)

    conn = connect()
    account_map = _load_account_map(conn)
    asset_set = _load_asset_set(conn)

    rows = _parse_csv(Path(args.file))
    valid, errors = _validate(rows, account_map, asset_set)

    if errors:
        for e in errors:
            log.error(
                "validation_error", extra={"line": e["line"], "errors": e["errors"]}
            )
        log.error("import_aborted", extra={"error_count": len(errors)})
        sys.exit(1)

    log.info("validation_ok", extra={"rows": len(valid)})

    if args.dry_run:
        for r in valid:
            log.info(
                "dry_run_row",
                extra={
                    "date": str(cut_date),
                    "account_id": r["account_id"],
                    "asset": r["asset"],
                    "amount": str(r["amount"]),
                },
            )
        log.info("dry_run_complete", extra={"rows": len(valid)})
        sys.exit(0)

    written = _upsert(conn, cut_date, valid)
    log.info("import_done", extra={"date": str(cut_date), "rows": written})
    conn.close()


if __name__ == "__main__":
    main()
