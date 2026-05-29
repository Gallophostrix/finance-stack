"""
Compute Time-Weighted Return (TWR) and Money-Weighted Return (MWR)
from derived.balances_eur and derived.flows_eur.

Granularity:
  - portfolio  : global TWR + MWR
  - category   : per asset class (crypto, actions, epargne) TWR + MWR
  - asset      : per asset TWR only

Results stored in derived.returns_itd.
All returns are ITD (Inception To Date) from the earliest available date.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from dateutil.relativedelta import relativedelta
from psycopg import Connection as PGConnection

log = logging.getLogger("root")

# Newton's method convergence settings
MAX_ITER = 1000
TOLERANCE = Decimal("1e-10")


# ---------- DB queries ----------


def _portfolio_snapshots(conn: PGConnection) -> list[tuple[date, Decimal]]:
    """Monthly portfolio values, ordered by date."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d, SUM(value_eur) AS total
            FROM derived.balances_eur
            GROUP BY d
            ORDER BY d
        """)
        return [(d, Decimal(str(v))) for d, v in cur.fetchall()]


def _category_snapshots(
    conn: PGConnection,
    category: str,
) -> list[tuple[date, Decimal]]:
    """Monthly portfolio values for a given asset class."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.d, SUM(b.value_eur) AS total
            FROM derived.balances_eur b
            JOIN core.assets a ON a.asset_code = b.asset
            WHERE a.class = %s
            GROUP BY b.d
            ORDER BY b.d
        """,
            (category,),
        )
        return [(d, Decimal(str(v))) for d, v in cur.fetchall()]


def _category_snapshots_filtered(
    conn: PGConnection,
    category: str,
    exclude: list[str],
) -> list[tuple[date, Decimal]]:
    placeholders = ",".join(["%s"] * len(exclude))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT b.d, SUM(b.value_eur) AS total
            FROM derived.balances_eur b
            JOIN core.assets a ON a.asset_code = b.asset
            WHERE a.class = %s
              AND b.asset NOT IN ({placeholders})
            GROUP BY b.d
            ORDER BY b.d
        """,
            [category] + exclude,
        )
        return [(d, Decimal(str(v))) for d, v in cur.fetchall()]


def _asset_snapshots(
    conn: PGConnection,
    asset: str,
) -> list[tuple[date, Decimal]]:
    """Monthly values for a single asset across all accounts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d, SUM(value_eur) AS total
            FROM derived.balances_eur
            WHERE asset = %s
            GROUP BY d
            ORDER BY d
        """,
            (asset,),
        )
        return [(d, Decimal(str(v))) for d, v in cur.fetchall()]


def _flows_between(
    conn: PGConnection,
    from_date: date,
    to_date: date,
    category: Optional[str] = None,
    asset: Optional[str] = None,
    exclude_interest: bool = False,
    exclude_assets: Optional[list[str]] = None,
) -> list[tuple[date, Decimal]]:
    """
    Net flows (in - out) between two dates.
    Returns [(date, signed_amount)] sorted by date.
    """
    filters = ["f.d >= %s", "f.d <= %s"]
    params = [from_date, to_date]
    interest_val = "0" if exclude_interest else "f.amount_eur"

    if category:
        filters.append("a.class = %s")
        params.append(category)
    if asset:
        filters.append("f.asset = %s")
        params.append(asset)
    if exclude_assets:
        placeholders = ",".join(["%s"] * len(exclude_assets))
        filters.append(f"f.asset NOT IN ({placeholders})")
        params.extend(exclude_assets)

    where = " AND ".join(filters)
    join = "JOIN core.assets a ON a.asset_code = f.asset" if category else ""

    sql = f"""
    SELECT f.d,
           SUM(CASE f.kind
               WHEN 'in'       THEN  f.amount_eur
               WHEN 'interest' THEN  {interest_val}
               WHEN 'out'      THEN -f.amount_eur
               ELSE 0
           END) AS net_flow
    FROM derived.flows_eur f
    {join}
    WHERE {where}
    GROUP BY f.d
    ORDER BY f.d
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(d, Decimal(str(v))) for d, v in cur.fetchall()]


def _active_assets(conn: PGConnection) -> list[tuple[str, str]]:
    """Returns [(asset_code, class)] for all active assets with balance data."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT a.asset_code, a.class
            FROM core.assets a
            JOIN derived.balances_eur b ON b.asset = a.asset_code
            WHERE a.is_active = TRUE
            ORDER BY a.class, a.asset_code
        """)
        return cur.fetchall()


# ---------- TWR ----------


def compute_twr(
    snapshots: list[tuple[date, Decimal]],
    flows: list[tuple[date, Decimal]],
) -> Optional[Decimal]:
    """
    Compute Time-Weighted Return from monthly snapshots and flows.

    Sub-period return: (V_end) / (V_start + flows_in_period) - 1
    TWR = product of (1 + sub_period_return) - 1
    """
    if len(snapshots) < 2:
        return None

    flows_by_date: dict[date, Decimal] = {}
    for d, amount in flows:
        flows_by_date[d] = flows_by_date.get(d, Decimal("0")) + amount

    twr = Decimal("1")
    for i in range(1, len(snapshots)):
        d_start, v_start = snapshots[i - 1]
        d_end, v_end = snapshots[i]

        # Sum flows between d_start and d_end
        period_flows = sum(
            v for d, v in flows_by_date.items() if d_start <= d < d_end
        ) or Decimal("0")

        denominator = v_start + period_flows
        if denominator <= 0:
            log.warning(
                "twr_skip_period",
                extra={
                    "d_start": str(d_start),
                    "d_end": str(d_end),
                    "denominator": str(denominator),
                },
            )
            continue

        sub_return = v_end / denominator

        twr *= sub_return

    return twr - Decimal("1")


def _compute_category_twr_weighted(
    conn: PGConnection,
    category: str,
    exclude_assets: Optional[list[str]] = None,
) -> Optional[Decimal]:
    """
    By class TWR = weighted average of individual TWR assets.
    Weight = average value over the period (V0 + Vn) / 2.
    Excludes assets without a calculable TWR (< 2 snapshots).
    """
    exclude_assets = exclude_assets or []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT a.asset_code
            FROM core.assets a
            JOIN derived.balances_eur b ON b.asset = a.asset_code
            WHERE a.class = %s AND a.is_active = TRUE
            """,
            (category,),
        )
        assets = [row[0] for row in cur.fetchall() if row[0] not in exclude_assets]

    weighted_sum = Decimal("0")
    total_weight = Decimal("0")

    for asset_code in assets:
        snaps = _asset_snapshots(conn, asset_code)
        if len(snaps) < 2:
            continue
        a_start = snaps[0][0]
        a_end = snaps[-1][0]
        flows = _flows_between(conn, a_start, a_end, asset=asset_code)
        twr = compute_twr(snaps, flows)
        if twr is None:
            continue

        v0 = snaps[0][1]
        vn = snaps[-1][1]
        weight = (v0 + vn) / Decimal("2")
        weighted_sum += twr * weight
        total_weight += weight

    if total_weight == 0:
        return None
    return weighted_sum / total_weight


# ---------- MWR (IRR via Newton's method) ----------


def compute_mwr(
    snapshots: list[tuple[date, Decimal]],
    flows: list[tuple[date, Decimal]],
) -> Optional[Decimal]:
    """
    Compute Money-Weighted Return (IRR) using Newton's method.

    Cash flows:
      - t=0 : -V0 (initial investment, negative = outflow from investor)
      - ti  : +Fi or -Fi (net flows)
      - t=n : +Vn (terminal value, positive = return to investor)

    Solve: NPV(r) = 0
    """
    if len(snapshots) < 2:
        return None

    d_start = snapshots[0][0]
    d_end = snapshots[-1][0]
    v_start = snapshots[0][1]
    v_end = snapshots[-1][1]

    # Build cash flow series: (time_in_years, amount)
    # Initial: investor "put in" v_start at t=0
    cf: list[tuple[Decimal, Decimal]] = [(Decimal("0"), -v_start)]

    # Intermediate flows
    for d, amount in flows:
        if d < d_start or d >= d_end:
            continue
        d_adjusted = d + relativedelta(months=1)
        t = Decimal(str((d_adjusted - d_start).days)) / Decimal("365")
        cf.append((t, -amount))  # negative = investor put in money

    # Terminal: investor "receives" v_end at t=n
    t_end = Decimal(str((d_end - d_start).days)) / Decimal("365")
    cf.append((t_end, v_end))

    if t_end <= 0:
        return None

    def npv(r: Decimal) -> Decimal:
        total = Decimal("0")
        try:
            for t, c in cf:
                total += c / (1 + r) ** t
        except Exception:
            return Decimal("999999")
        return total

    def npv_deriv(r: Decimal) -> Decimal:
        total = Decimal("0")
        try:
            for t, c in cf:
                if t > 0:
                    total -= t * c / (1 + r) ** (t + 1)
        except Exception:
            return Decimal("1")
        return total

    # Initial guess — try multiple starting points
    for initial_r in [
        Decimal("-0.8"),
        Decimal("0.05"),
        Decimal("-0.5"),
        Decimal("0.5"),
        Decimal("-0.1"),
    ]:
        r = initial_r
        for _ in range(MAX_ITER):
            try:
                f = npv(r)
                fp = npv_deriv(r)
                if abs(fp) < Decimal("1e-12"):
                    break
                r_new = r - f / fp
                if r_new < Decimal("-0.99"):
                    r_new = Decimal("-0.99")
                if r_new > Decimal("10"):
                    r_new = Decimal("10")
                if abs(r_new - r) < TOLERANCE:
                    if abs(r_new) > Decimal("9.99"):
                        break  # diverged, try next starting point
                    return r_new
                r = r_new
            except Exception:
                break

        log.warning("mwr_no_convergence")
        return None


# ---------- Upsert ----------


def _upsert_return(
    conn: PGConnection,
    as_of: date,
    metric: str,
    level: str,
    category: str,
    asset: str,
    value: Optional[Decimal],
) -> None:
    if value is None:
        return
    sql = """
    INSERT INTO derived.returns_itd
        (as_of, metric, level, category, asset, value, computed_at)
    VALUES (%s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (as_of, metric, level, category, asset) DO UPDATE
        SET value       = EXCLUDED.value,
            computed_at = EXCLUDED.computed_at
    """
    with conn.cursor() as cur:
        cur.execute(sql, (as_of, metric, level, category, asset, value))


# ---------- Main ----------


def run(conn: PGConnection) -> dict:
    """Compute and store all returns."""
    snapshots_all = _portfolio_snapshots(conn)
    if len(snapshots_all) < 2:
        log.warning("returns_insufficient_data")
        return {}

    as_of = snapshots_all[-1][0]
    d_start = snapshots_all[0][0]
    d_end_flows = snapshots_all[-2][0]
    categories = ["crypto", "actions", "epargne"]

    counts = {"twr": 0, "mwr": 0}

    # ── Global portfolio ──────────────────────────────────────────
    flows_twr = _flows_between(conn, d_start, d_end_flows, exclude_interest=False)
    flows_mwr = _flows_between(conn, d_start, d_end_flows, exclude_interest=True)

    twr = compute_twr(snapshots_all, flows_twr)
    mwr = compute_mwr(snapshots_all, flows_mwr)

    _upsert_return(conn, as_of, "twr", "category", "portfolio", "", twr)
    _upsert_return(conn, as_of, "mwr", "category", "portfolio", "", mwr)
    if twr is not None:
        counts["twr"] += 1
    if mwr is not None:
        counts["mwr"] += 1

    log.info(
        "returns_portfolio",
        extra={
            "twr": str(twr)[:8] if twr else None,
            "mwr": str(mwr)[:8] if mwr else None,
        },
    )

    # ── By class ────────────────────────────────────────────────
    for cat in categories:
        exclude = ["COMPTE_COURANT", "ESPECES"]

        twr = _compute_category_twr_weighted(conn, cat, exclude_assets=exclude)

        if cat == "epargne":
            snaps = _category_snapshots_filtered(conn, cat, exclude)
            flows_mwr = _flows_between(
                conn,
                d_start,
                d_end_flows,
                category=cat,
                exclude_assets=exclude,
                exclude_interest=True,
            )
        else:
            snaps = _category_snapshots(conn, cat)
            flows_mwr = _flows_between(
                conn, d_start, d_end_flows, category=cat, exclude_interest=True
            )

        if len(snaps) >= 2:
            mwr = compute_mwr(snaps, flows_mwr)
        else:
            mwr = None

        _upsert_return(conn, as_of, "twr", "category", cat, "", twr)
        _upsert_return(conn, as_of, "mwr", "category", cat, "", mwr)
        if twr is not None:
            counts["twr"] += 1
        if mwr is not None:
            counts["mwr"] += 1

        log.info(
            "returns_category",
            extra={
                "category": cat,
                "twr": str(twr)[:8] if twr else None,
                "mwr": str(mwr)[:8] if mwr else None,
            },
        )

    # ── By asset ─────────────────────────────────────────────────
    for asset_code, asset_class in _active_assets(conn):
        EXCLUDE_FROM_ASSET_TWR = {"COMPTE_COURANT", "ESPECES"}
        if asset_code in EXCLUDE_FROM_ASSET_TWR:
            continue

        snaps = _asset_snapshots(conn, asset_code)
        if len(snaps) < 2:
            continue
        a_start = snaps[0][0]
        a_end = snaps[-1][0]
        flows = _flows_between(conn, a_start, a_end, asset=asset_code)
        twr = compute_twr(snaps, flows)
        if twr is None:
            continue

        _upsert_return(conn, as_of, "twr", "asset", asset_class, asset_code, twr)
        counts["twr"] += 1

        log.info("returns_asset", extra={"asset": asset_code, "twr": str(twr)[:8]})

    conn.commit()
    log.info("returns_done", extra=counts)
    return counts
