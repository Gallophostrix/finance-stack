import os, sys, glob, yaml, time, datetime as dt, requests, psycopg2
from loaders.pg import upsert_valuation, upsert_flow
from utils.dates import start_of_month
from adapters import bitcoin as btc
from adapters import cardano as ada

ADAPTERS = {
  "bitcoin": btc,
  "cardano": ada,
  # "xrp": xrp,  # plus tard
}

_HIST_PRICE_CACHE = {}

def get_as_of(arg):
    if arg and arg.lower() != "auto":
        return dt.datetime.strptime(arg, "%Y-%m-%d").date()
    return start_of_month(dt.date.today())

def load_cfgs(path="/assets"):
    cfg = {"common": {}, "chains": []}
    with open(os.path.join(path, "common.yml"), "r") as f:
        cfg["common"] = yaml.safe_load(f) or {}
    for fp in glob.glob(os.path.join(path, "*.yml")):
        if fp.endswith("common.yml"): continue
        with open(fp, "r") as f:
            cfg["chains"].append(yaml.safe_load(f) or {})
    return cfg

def cg_simple_price(ids, vs=("eur",), retries=3, pause=1.0):
    if not ids: return {}
    url = "https://api.coingecko.com/api/v3/simple/price"
    ids = ",".join(sorted(set(ids)))
    vs  = ",".join(vs)
    for _ in range(retries):
        r = requests.get(url, params={"ids": ids, "vs_currencies": vs}, timeout=20)
        if r.status_code == 429:
            time.sleep(pause); continue
        r.raise_for_status()
        return r.json()

def cg_date_price(cid, day: dt.date, vs="eur", retries=3, pause=1.0):
    """
    Fetch the coin price at a given date.
    """
    if not cid or not isinstance(day, dt.date):
        return None

    key = (cid, day.isoformat(), vs)
    if key in _HIST_PRICE_CACHE:
        return _HIST_PRICE_CACHE[key]

    url = f"https://api.coingecko.com/api/v3/coins/{cid}/history"
    params = {"date": day.strftime("%d-%m-%Y"), "localization": "false"}
    last_err = None
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(pause); continue
            r.raise_for_status()
            data = r.json()
            px = float(data["market_data"]["current_price"][vs])
            _HIST_PRICE_CACHE[key] = px
            return px
        except Exception as e:
            time.sleep(pause)
    return None

def main():
    # args: --as-of YYYY-MM-DD | auto
    as_of_arg = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--as-of":
        as_of_arg = sys.argv[2]
    as_of = get_as_of(as_of_arg)
    start_of_month = as_of.replace(day=1)

    cfg = load_cfgs("/assets")
    base_quote = cfg["common"].get("base_quote", "EUR").upper()
    if base_quote != "EUR":
        raise SystemExit("Only EUR base_quote supported in this version.")

    # 1) Collect balances & pricing meta from adapters
    balances = {}     # {symbol: qty}
    pricing = {}      # {symbol: {"pricing":..., "coingecko_id":...}}
    subcats = {}      # {symbol: subcategory} (to write in valuations)

    for chain_cfg in cfg["chains"]:
        chain = chain_cfg.get("chain")
        subcat = chain_cfg.get("subcategory", "Wallet")

        mod = ADAPTERS.get(chain)
        if not mod: 
            continue
        fetcher = getattr(mod, "fetch", None)
        if not callable(fetcher):
            continue

        b, meta = fetcher(chain_cfg)
        # always propagate subcategory for symbols returned by this chain
        for sym, qty in b.items():
            balances[sym] = balances.get(sym, 0.0) + float(qty)
            subcats[sym] = subcat
        for sym, m in meta.items():
            pricing[sym] = m

    # 2) Prepare CoinGecko IDs & fetch prices once
    cg_ids = [m["coingecko_id"] for m in pricing.values() if m.get("coingecko_id")]
    price_map = cg_simple_price(cg_ids, vs=("eur",)) if cg_ids else {}

    # 3) Compute EUR valuations for ALL symbols (zeros included)
    valuations = []  # list[(sym, value_eur, subcat)]
    for sym, qty in balances.items():
        m = pricing.get(sym, {})
        mode = m.get("pricing", "auto")
        cid  = m.get("coingecko_id")
        px_eur = None

        if mode == "manual":
            px_eur = float(m["fixed_eur"])
        else:  # auto
            px_eur = price_map.get(cid, {}).get("eur") if cid else None

        # if still None: set 0.0 (explicitly no price) so DB gets zeroed
        val_eur = round(float(qty) * float(px_eur), 2) if (px_eur is not None) else 0.0
        valuations.append((sym, val_eur, subcats.get(sym, "Wallet")))

    # 4) Flows
    flows_to_insert = []

    for chain_cfg in cfg["chains"]:
        chain = chain_cfg.get("chain")
        mod = ADAPTERS.get(chain)
        if not mod:
            continue

        flows_fn = getattr(mod, "flows_detection", None)
        if not callable(flows_fn):
            continue

        native_flows = flows_fn(chain_cfg, start_of_month, as_of)

        for f in native_flows:
            sym = f["asset"]  # ex: "BTC", "ADA"
            # On retrouve l’ID CoinGecko pour CET asset via le dict pricing déjà construit
            cid = (pricing.get(sym) or {}).get("coingecko_id")
            
            px_eur = cg_date_price(cid, f["d"]) if cid else None
            if px_eur is None:
                px_eur = (price_map.get(cid, {}) or {}).get("eur", 0.0) if cid else 0.0

            amt_eur = round(abs(float(f["amount_native"])) * float(px_eur), 2)

            flows_to_insert.append((
                f["d"],
                f.get("category", "tokens"),
                f.get("subcategory", "Wallet"),
                sym,
                amt_eur,
                f["kind"],
                f.get("tx_hash")  # optional
            ))

    # 5) UPSERT into valuations
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST","finance-db"),
        port=int(os.getenv("DB_PORT","5432")),
        dbname=os.getenv("DB_NAME","finance"),
        user=os.getenv("DB_USER","finance"),
        password=os.getenv("DB_PASS",""),
    )
    cur = conn.cursor()

    for sym, value_eur, subcat in valuations:
        upsert_valuation(cur, as_of, "tokens", subcat, sym, value_eur)

    for (d, cat, subcat, asset, amt_eur, kind, txh) in flows_to_insert:
        try:
            # recommended hash version
            upsert_flow(cur, d, cat, subcat, asset, amt_eur, kind, txh)
        except TypeError:
            # hash ignored/unavailable
            upsert_flow(cur, d, cat, subcat, asset, amt_eur, kind)

    conn.commit()
    cur.close(); conn.close()

if __name__ == "__main__":
    main()
