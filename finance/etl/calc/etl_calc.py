import os
import numpy_financial as npf
import pandas as pd
import psycopg2

DB_HOST = os.getenv("DB_HOST", "finance-db")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "finance")
DB_USER = os.getenv("DB_USER", "finance")
DB_PASS = os.getenv("DB_PASS", "")

KINDS_EXTERNAL = ("in","out","fee")   # interest exclu (perf)

def xirr(dates, amounts):
    """Renvoie l'IRR (en décimal) ou None si impossible.
       dates: list[date], amounts: list[float] (nég = invest, pos = récup)"""
    if not dates or not amounts or len(dates) != len(amounts):
        return None
    if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
        return None  # besoin d'au moins un flux neg et un pos
    # numpy_financial.xirr attend des datetime-like et montants
    try:
        return float(npf.xirr(amounts, pd.to_datetime(dates)))
    except Exception:
        # fallback binaire simple sur r in [-0.9999, 10] (très large)
        lo, hi = -0.9999, 10.0
        def npv(r):
            t0 = dates[0]
            return sum(a / ((1+r) ** ((d - t0).days / 365.2425)) for a, d in zip(amounts, dates))
        try:
            for _ in range(80):
                mid = (lo+hi)/2
                v = npv(mid)
                if abs(v) < 1e-8: return mid
                # signe à t0 fixé: si v>0, taux trop bas -> monte
                if v > 0: lo = mid
                else: hi = mid
            return mid
        except Exception:
            return None

def fetch_df(cur, sql, params=None):
    cur.execute(sql, params or ())
    cols = [d.name for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)

def compute_mwr_for_group(flow_df, val_df, group_cols, label_value, level, cur):
    """Calcule MWR mensuel pour chaque groupe (asset ou category) et upsert dans mwr_results."""
    # Prépare les dates mois disponibles (fin de mois = d)
    months = sorted(val_df["m"].unique())
    if not months:
        return 0
    upserts = 0
    for m in months:
        # Cash flows externes jusqu'à m (in/out/fee) avec signes: in -, out +, fee -
        flows_m = flow_df[flow_df["d"] <= m].copy()
        if flows_m.empty:
            flows_m = flows_m  # no-op
        # Montants signés
        def signed(row):
            k, a = row["kind"], float(row["amount_eur"])
            if k == "in":   return -a
            if k == "out":  return +a
            if k == "fee":  return -a
            return 0.0
        if not flows_m.empty:
            flows_m["amt"] = flows_m.apply(signed, axis=1)
        # Terminal value: + valeur au mois m
        vals_at_m = val_df[val_df["m"] == m]["value_eur"].sum()
        # Construit séries dates/amounts
        dates = []
        amts  = []
        if not flows_m.empty:
            # agrège par date (pour stabilité)
            agg = flows_m.groupby("d", as_index=False)["amt"].sum().sort_values("d")
            dates.extend(pd.to_datetime(agg["d"]).dt.date.tolist())
            amts.extend(agg["amt"].tolist())
        dates.append(pd.to_datetime(m).date())
        amts.append(float(vals_at_m))
        irr = xirr(dates, amts)
        # UPSERT
        cat_val   = label_value if level == "category" else ""
        asset_val = label_value if level == "asset"    else ""

        cur.execute("""
	  INSERT INTO mwr_results (as_of, level, category, asset, irr)
	  VALUES (%s,%s,%s,%s,%s)
	  ON CONFLICT ON CONSTRAINT mwr_results_pkey
	  DO UPDATE SET irr = EXCLUDED.irr
        """, (m, level, cat_val, asset_val, irr))
        upserts += 1
    return upserts

def main():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )
    conn.autocommit = False
    cur = conn.cursor()

    # --- Par ASSET (toutes catégories confondues, on garde "category" en filtre dans SQL si tu veux limiter) ---
    # Valuations par mois & asset
    val_asset = fetch_df(cur, """
        SELECT date_trunc('month', d)::date AS m, asset, SUM(value_eur) AS value_eur
        FROM valuations
        GROUP BY 1,2
    """)
    # Flows externes par date/asset
    flow_asset = fetch_df(cur, """
        SELECT d::date, asset, kind, SUM(amount_eur) AS amount_eur
        FROM flows
        WHERE kind IN %s
        GROUP BY 1,2,3
        ORDER BY 1
    """, (KINDS_EXTERNAL,))
    if not val_asset.empty:
        for asset in sorted(val_asset["asset"].unique()):
            vdf = val_asset[val_asset["asset"] == asset]
            fdf = flow_asset[flow_asset["asset"] == asset]
            compute_mwr_for_group(fdf.rename(columns={"d":"d"}), vdf, ["asset"], asset, "asset", cur)

    # --- Par CATEGORIE (liquidites / actions / tokens) ---
    val_cat = fetch_df(cur, """
        SELECT date_trunc('month', d)::date AS m, category, SUM(value_eur) AS value_eur
        FROM valuations
        GROUP BY 1,2
    """)
    flow_cat = fetch_df(cur, """
        SELECT d::date, category, kind, SUM(amount_eur) AS amount_eur
        FROM flows
        WHERE kind IN %s
        GROUP BY 1,2,3
        ORDER BY 1
    """, (KINDS_EXTERNAL,))
    if not val_cat.empty:
        for cat in sorted(val_cat["category"].unique()):
            vdf = val_cat[val_cat["category"] == cat]
            fdf = flow_cat[flow_cat["category"] == cat]
            compute_mwr_for_group(fdf.rename(columns={"d":"d"}), vdf, ["category"], cat, "category", cur)

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
