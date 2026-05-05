#!/usr/bin/env python3
"""
Import flows from a CSV file into core.flows_native.

CSV format:
  account_label,asset,amount,kind,ref
  "Lucya Cardif AV account",WORLD,500.00,in,virement-jan
  "Bourse Direct PEA account",WORLD,200.00,out,retrait-jan

  kind : in | out | fee | interest
  ref  : obligatoire — libellé libre (ou tx hash pour crypto)

Usage:
  python scripts/import_flows.py --date 2025-01-15 --file finance/data/flows/2025-01-15.csv
  python scripts/import_flows.py --date 2025-01-15 --file finance/data/flows/2025-01-15.csv --dry-run
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

VALID_KINDS = {"in", "out", "fee", "interest"}


# ---------- DB helpers ----------


def _load_account_map(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT account_id, label FROM core.accounts WHERE is_active")
        mapping = {label: aid for aid, label in cur.fetchall()}
    log.info("account_map_loaded", extra={"count": len(mapping)})
    return mapping


def _load_asset_set(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT asset_code FROM core.assets WHERE is_active")
        assets = {r[0] for r in cur.fetchall()}
    log.info("asset_set_loaded", extra={"count": len(assets)})
    return assets


def _load_provider_map(conn) -> dict[int, str]:
    """account_id -> provider_name."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, p.provider_name
            FROM core.accounts a
            JOIN core.providers p USING (provider_id)
        """)
        return {aid: pname for aid, pname in cur.fetchall()}


# ---------- flow_uid ----------


def _make_flow_uid(
    provider_name: str, account_id: int, asset: str, d: date, kind: str, ref: str
) -> str:
    """
    Deterministic unique ID for a manual flow.
    ref is mandatory — prevents silent deduplication of same-day flows.
    Format: manual:{provider}:{account_id}:{asset}:{date}:{kind}:{ref}
    """
    return f"manual:{provider_name}:{account_id}:{asset}:{d}:{kind}:{ref}"


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
                    "kind": row["kind"].strip().lower(),
                    "ref": row.get("ref", "").strip(),
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
        if r["kind"] not in VALID_KINDS:
            errs.append(f"invalid kind '{r['kind']}' — valid: {sorted(VALID_KINDS)}")
        if not r["ref"]:
            errs.append("ref is mandatory")
        try:
            amount = Decimal(r["amount"])
            if amount <= 0:
                errs.append("amount must be > 0")
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
                    "kind": r["kind"],
                    "ref": r["ref"],
                }
            )
    return valid, errors


# ---------- DB write ----------


def _upsert(conn, flow_date: date, rows: list, provider_map: dict) -> tuple[int, int]:
    sql = """
    INSERT INTO core.flows_native
      (flow_uid, d, account_id, asset, amount_native, kind, origin_ref)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (flow_uid) DO NOTHING
    """
    inserted, skipped = 0, 0
    with conn.cursor() as cur:
        for r in rows:
            provider_name = provider_map.get(r["account_id"], "unknown")
            uid = _make_flow_uid(
                provider_name,
                r["account_id"],
                r["asset"],
                flow_date,
                r["kind"],
                r["ref"],
            )
            cur.execute(
                sql,
                (
                    uid,
                    flow_date,
                    r["account_id"],
                    r["asset"],
                    r["amount"],
                    r["kind"],
                    r["ref"],
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
                log.warning("flow_skipped_duplicate", extra={"uid": uid})
    conn.commit()
    return inserted, skipped


# ---------- Main ----------


def main() -> None:
    ap = argparse.ArgumentParser(description="Import flows from CSV")
    ap.add_argument("--date", required=True, help="Flow date YYYY-MM-DD")
    ap.add_argument("--file", required=True, help="Path to CSV file")
    ap.add_argument("--dry-run", action="store_true", help="Validate only, no DB write")
    args = ap.parse_args()

    try:
        flow_date = date.fromisoformat(args.date)
    except ValueError:
        log.error("invalid_date", extra={"date": args.date})
        sys.exit(1)

    conn = connect()
    account_map = _load_account_map(conn)
    asset_set = _load_asset_set(conn)
    provider_map = _load_provider_map(conn)

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
            provider_name = provider_map.get(r["account_id"], "unknown")
            uid = _make_flow_uid(
                provider_name,
                r["account_id"],
                r["asset"],
                flow_date,
                r["kind"],
                r["ref"],
            )
            log.info("dry_run_row", extra={"uid": uid, "amount": str(r["amount"])})
        log.info("dry_run_complete", extra={"rows": len(valid)})
        sys.exit(0)

    inserted, skipped = _upsert(conn, flow_date, valid, provider_map)
    log.info(
        "import_done",
        extra={"date": str(flow_date), "inserted": inserted, "skipped": skipped},
    )
    conn.close()


if __name__ == "__main__":
    main()
