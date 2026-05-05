# etl/api/etl_calc.py
from __future__ import annotations

import argparse
from datetime import date
from typing import Dict, List, Optional, Tuple

from etl.utils.dates import today_utc_date
from etl.utils.db import connect_with_retry
from etl.utils.logging import setup_json_logging
from psycopg2.extras import execute_values  # type: ignore

# --------- Configuration ---------
# TWR: neutralizing external flows (in/out/fee). Interest is treated as performance (not neutralized).
KINDS_TWR_EXTERNAL = ("in", "out", "fee")

# MWR: including interest as a positive cashflow for the investor experience.
KINDS_MWR = ("in", "out", "fee", "interest")

ASSETS_CLASS_COL = "class"
EPS = 1e-12


# ----------------- XIRR core -----------------
def _npv(rate: float, t0: date, flows: List[Tuple[date, float]]) -> float:
    """
    Compute the Net Present Value function for irregular-dated cashflows (XNPV).

    Args:
        rate: Discount rate (annual, decimal). Must be > -1.
        t0: Reference date (usually the earliest cashflow date).
        flows: List of (date, amount) tuples using investor sign convention.

    Returns:
        NPV(rate) as a float.

    Notes:
        Uses ACT/365.2425 day count to approximate civil years.
    """
    denom = 365.2425
    s = 0.0
    for d, a in flows:
        dt = (d - t0).days / denom
        s += a / ((1.0 + rate) ** dt)
    return s


def xirr_bisection(flows: List[Tuple[date, float]]) -> Optional[float]:
    """
    Solve XIRR (money-weighted return) via bisection, i.e., find r such that NPV(r)=0.

    Args:
        flows: List[(date, amount)] using investor convention:
               - contributions negative,
               - withdrawals positive,
               - fees negative,
               - interests positive,
               - terminal value at as_of positive.

    Returns:
        Annualized IRR as a decimal, or None if not solvable / not bracketable.
    """
    if not flows:
        return None

    has_neg = any(a < 0 for _, a in flows)
    has_pos = any(a > 0 for _, a in flows)
    if not (has_neg and has_pos):
        return None

    flows = sorted(flows, key=lambda x: x[0])
    t0 = flows[0][0]

    r_lo = -0.9999
    r_hi = 0.1

    try:
        f_lo = _npv(r_lo, t0, flows)
        f_hi = _npv(r_hi, t0, flows)
    except Exception:
        return None

    # Bracket expansion: grow r_hi until NPV changes sign (or bail out).
    for _ in range(80):
        if f_lo == 0.0:
            return r_lo
        if f_hi == 0.0:
            return r_hi
        if (f_lo > 0 and f_hi < 0) or (f_lo < 0 and f_hi > 0):
            break
        r_hi *= 2.0
        if r_hi > 1e6:
            return None
        try:
            f_hi = _npv(r_hi, t0, flows)
        except Exception:
            return None
    else:
        return None

    # Bisection within bracket
    for _ in range(120):
        mid = (r_lo + r_hi) / 2.0
        try:
            f_mid = _npv(mid, t0, flows)
        except Exception:
            return None

        if abs(f_mid) < 1e-10:
            return mid

        if (f_lo > 0 and f_mid < 0) or (f_lo < 0 and f_mid > 0):
            r_hi = mid
            f_hi = f_mid
        else:
            r_lo = mid
            f_lo = f_mid

    return (r_lo + r_hi) / 2.0


# ----------------- DB helper -----------------
def fetch_all(cur, sql: str, params: Tuple = ()) -> List[Tuple]:
    """
    Execute a SQL query and return all rows.
    """
    cur.execute(sql, params)
    return list(cur.fetchall())


# ----------------- TWR computation -----------------
def compute_twr_itd_by_points(
    series: List[Tuple[date, str, str, float, float]],
) -> Dict[Tuple[str, str, date], float]:
    """
    Compute ITD Time-Weighted Return (TWR) from discrete valuation points.

    Args:
        series: rows (as_of, category, asset, V_t, F_t) ordered arbitrarily.
            - as_of: valuation date (month-start snapshots).
            - V_t: valuation at as_of.
            - F_t: external net flow aligned to the interval ending at as_of, with:
                in  -> +amount
                out -> -amount
                fee -> -amount
              (interest excluded => treated as performance via V_t)

    Returns:
        (category, asset, as_of) -> twr_itd at as_of (decimal).
    """
    grouped: Dict[Tuple[str, str], List[Tuple[date, float, float]]] = {}
    for as_of, cat, asset, v, f in series:
        grouped.setdefault((cat, asset), []).append((as_of, float(v), float(f)))

    out: Dict[Tuple[str, str, date], float] = {}
    for (cat, asset), rows in grouped.items():
        rows.sort(key=lambda x: x[0])

        prev_v: Optional[float] = None
        cum = 1.0

        for as_of, v, f in rows:
            if prev_v is None:
                prev_v = v
                out[(cat, asset, as_of)] = 0.0
                continue

            if abs(prev_v) < EPS:
                prev_v = v
                out[(cat, asset, as_of)] = cum - 1.0
                continue

            r = (v - f) / prev_v - 1.0
            cum *= 1.0 + r
            prev_v = v
            out[(cat, asset, as_of)] = cum - 1.0

    return out


def upsert_returns(cur, rows: List[Tuple[date, str, str, str, str, float]]) -> None:
    """
    Bulk upsert computed return metrics into derived.returns_itd.

    rows: (as_of, metric, level, category, asset, value)
    """
    sql = """
    INSERT INTO derived.returns_itd (as_of, metric, level, category, asset, value)
    VALUES %s
    ON CONFLICT (as_of, metric, level, category, asset)
    DO UPDATE SET
      value = EXCLUDED.value,
      computed_at = NOW();
    """
    execute_values(cur, sql, rows, page_size=1000)


def _default_as_of_month_start() -> date:
    """
    Default 'as_of' is the first day of the current UTC month.
    """
    t = today_utc_date()
    return date(t.year, t.month, 1)


# ----------------- Main job -----------------
def run(*, dsn: Optional[str] = None, as_of: Optional[date] = None) -> None:
    """
    Compute and persist ITD return metrics up to a chosen as_of date.

    Behavior:
      - If as_of is provided, all computations are restricted to dates <= as_of.
      - Only results for that exact as_of are persisted (no backfill).
      - The as_of is NOT snapped to last available snapshot (caller responsibility).

    Metrics stored for the given as_of:
      - TWR ITD by asset
      - TWR ITD by category
      - MWR ITD by category
    """
    log = setup_json_logging()
    conn = connect_with_retry(dsn)

    as_of = as_of or _default_as_of_month_start()

    try:
        with conn:
            with conn.cursor() as cur:
                # Ensure we actually have valuations exactly on as_of (since we do not snap).
                exists = fetch_all(
                    cur,
                    """
                    SELECT 1
                    FROM derived.balances_eur
                    WHERE d = %s
                    LIMIT 1;
                    """,
                    (as_of,),
                )
                if not exists:
                    log.info(
                        "returns_itd_no_snapshot_for_as_of",
                        extra={"step": "returns_itd", "as_of": str(as_of)},
                    )
                    return

                rows_to_upsert: List[Tuple[date, str, str, str, str, float]] = []

                # ============================================================
                # 1) TWR ITD (compute chain up to as_of, persist only as_of)
                # ============================================================

                # ---- 1.a) Asset-level TWR points (all month-starts <= as_of) ----
                asset_points = fetch_all(
                    cur,
                    f"""
                    WITH v AS (
                      SELECT
                        b.d::date AS as_of,
                        a.{ASSETS_CLASS_COL}::text AS category,
                        b.asset::text AS asset,
                        SUM(b.value_eur)::numeric AS value_eur
                      FROM derived.balances_eur b
                      JOIN core.assets a ON a.asset_code = b.asset
                      WHERE b.d <= %s
                        AND b.asset NOT IN ('COMPTE_COURANT')
                      GROUP BY 1,2,3
                    ),
                    f AS (
                      SELECT
                      date_trunc('month', (fe.d + interval '1 month'))::date AS as_of,
                        a.{ASSETS_CLASS_COL}::text AS category,
                        fe.asset::text AS asset,
                        SUM(
                          CASE fe.kind
                            WHEN 'in'  THEN  fe.amount_eur
                            WHEN 'out' THEN -fe.amount_eur
                            WHEN 'fee' THEN -fe.amount_eur
                            ELSE 0
                          END
                        )::numeric AS F_eur
                      FROM derived.flows_eur fe
                      JOIN core.assets a ON a.asset_code = fe.asset
                      WHERE fe.kind IN %s
                        AND fe.d <= %s
                        AND fe.asset NOT IN ('COMPTE_COURANT')
                      GROUP BY 1,2,3
                    )
                    SELECT
                      v.as_of,
                      v.category,
                      v.asset,
                      v.value_eur,
                      COALESCE(f.F_eur, 0)::numeric AS F_eur
                    FROM v
                    LEFT JOIN f
                      ON f.as_of = v.as_of
                     AND f.category = v.category
                     AND f.asset = v.asset
                    ORDER BY 1,2,3;
                    """,
                    (as_of, KINDS_TWR_EXTERNAL, as_of),
                )

                twr_asset = compute_twr_itd_by_points(asset_points)
                # Persist only the chosen as_of
                for (cat, asset, d), twr_itd in twr_asset.items():
                    if d == as_of:
                        rows_to_upsert.append(
                            (as_of, "twr", "asset", cat, asset, float(twr_itd))
                        )

                # ---- 1.b) Category-level TWR points ----
                cat_points = fetch_all(
                    cur,
                    f"""
                    WITH v AS (
                      SELECT
                        b.d::date AS as_of,
                        a.{ASSETS_CLASS_COL}::text AS category,
                        SUM(b.value_eur)::numeric AS value_eur
                      FROM derived.balances_eur b
                      JOIN core.assets a ON a.asset_code = b.asset
                      WHERE b.d <= %s
                      GROUP BY 1,2
                    ),
                    f AS (
                      SELECT
                      date_trunc('month', (fe.d + interval '1 month'))::date AS as_of,
                        a.{ASSETS_CLASS_COL}::text AS category,
                        SUM(
                          CASE fe.kind
                            WHEN 'in'  THEN  fe.amount_eur
                            WHEN 'out' THEN -fe.amount_eur
                            WHEN 'fee' THEN -fe.amount_eur
                            ELSE 0
                          END
                        )::numeric AS F_eur
                      FROM derived.flows_eur fe
                      JOIN core.assets a ON a.asset_code = fe.asset
                      WHERE fe.kind IN %s
                        AND fe.d <= %s
                      GROUP BY 1,2
                    )
                    SELECT
                      v.as_of,
                      v.category,
                      ''::text AS asset,
                      v.value_eur,
                      COALESCE(f.F_eur, 0)::numeric AS F_eur
                    FROM v
                    LEFT JOIN f
                      ON f.as_of = v.as_of
                     AND f.category = v.category
                    ORDER BY 1,2;
                    """,
                    (as_of, KINDS_TWR_EXTERNAL, as_of),
                )

                twr_cat = compute_twr_itd_by_points(cat_points)
                for (cat, _asset, d), twr_itd in twr_cat.items():
                    if d == as_of:
                        rows_to_upsert.append(
                            (as_of, "twr", "category", cat, "", float(twr_itd))
                        )

                # ============================================================
                # 2) MWR ITD (XIRR) by category, as_of terminal value
                # ============================================================

                categories = [
                    r[0]
                    for r in fetch_all(
                        cur,
                        f"""
                        SELECT DISTINCT a.{ASSETS_CLASS_COL}::text AS category
                        FROM core.assets a
                        ORDER BY 1;
                        """,
                    )
                ]

                flows_cat = fetch_all(
                    cur,
                    f"""
                    SELECT
                      fe.d::date AS d,
                      a.{ASSETS_CLASS_COL}::text AS category,
                      SUM(
                        CASE fe.kind
                          WHEN 'in'       THEN -fe.amount_eur
                          WHEN 'out'      THEN  fe.amount_eur
                          WHEN 'fee'      THEN -fe.amount_eur
                          WHEN 'interest' THEN  fe.amount_eur
                          ELSE 0
                        END
                      )::numeric AS cf
                    FROM derived.flows_eur fe
                    JOIN core.assets a ON a.asset_code = fe.asset
                    WHERE fe.kind IN %s
                      AND fe.d <= %s
                    GROUP BY 1,2
                    ORDER BY 2,1;
                    """,
                    (KINDS_MWR, as_of),
                )

                terminal_cat = fetch_all(
                    cur,
                    f"""
                    SELECT
                      a.{ASSETS_CLASS_COL}::text AS category,
                      SUM(b.value_eur)::numeric AS value_eur
                    FROM derived.balances_eur b
                    JOIN core.assets a ON a.asset_code = b.asset
                    WHERE b.d = %s
                    GROUP BY 1
                    ORDER BY 1;
                    """,
                    (as_of,),
                )

                flows_by_cat: Dict[str, List[Tuple[date, float]]] = {}
                for d, cat, cf in flows_cat:
                    flows_by_cat.setdefault(cat, []).append((d, float(cf)))

                terminal_by_cat: Dict[str, float] = {
                    cat: float(v) for cat, v in terminal_cat
                }

                for cat in categories:
                    tv = terminal_by_cat.get(cat)
                    if tv is None:
                        continue

                    cat_flows = flows_by_cat.get(cat, [])
                    # You may still compute MWR with only terminal value + at least one negative flow;
                    # we keep the standard requirement enforced by xirr_bisection().
                    if not cat_flows:
                        continue

                    cat_flows.sort(key=lambda x: x[0])
                    irr_flows = [(d, a) for (d, a) in cat_flows if d <= as_of] + [
                        (as_of, float(tv))
                    ]
                    irr = xirr_bisection(irr_flows)
                    if irr is None:
                        continue

                    rows_to_upsert.append(
                        (as_of, "mwr", "category", cat, "", float(irr))
                    )

                # ---- Persist only the chosen as_of ----
                if rows_to_upsert:
                    upsert_returns(cur, rows_to_upsert)

                log.info(
                    "returns_itd_done",
                    extra={
                        "step": "returns_itd",
                        "as_of": str(as_of),
                        "upserts": len(rows_to_upsert),
                    },
                )

    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_as_of(s: str) -> date:
    """
    Parse --as-of in ISO format YYYY-MM-DD and enforce month-start.
    """
    d = date.fromisoformat(s)
    if d.day != 1:
        raise ValueError(
            "--as-of must be a month-start date (YYYY-MM-01) in your snapshot convention."
        )
    return d


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ETL - Layer C: ITD returns at a chosen as_of (TWR assets/categories, MWR categories)"
    )
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (overrides PG_DSN)")
    ap.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Snapshot date to compute (YYYY-MM-DD, must be month-start). Default: first day of current UTC month.",
    )
    args = ap.parse_args()

    as_of = _parse_as_of(args.as_of) if args.as_of else None
    run(dsn=args.dsn, as_of=as_of)


if __name__ == "__main__":
    main()
