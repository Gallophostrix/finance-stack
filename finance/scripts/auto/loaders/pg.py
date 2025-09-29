def upsert_valuation(cur, d, category, subcategory, asset, value_eur):
    cur.execute("""
      INSERT INTO valuations (d, category, subcategory, asset, value_eur)
      VALUES (%s,%s,%s,%s,%s)
      ON CONFLICT (d, category, subcategory, asset)
      DO UPDATE SET value_eur = EXCLUDED.value_eur
    """, (d, category, subcategory, asset, value_eur))

def upsert_flow(cur, d, category, subcategory, asset, amount_eur, kind, tx_hash=None):
    cur.execute("""
        INSERT INTO flows (d, category, subcategory, asset, amount_eur, kind, tx_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        -- on cible l'index unique partiel (asset, tx_hash) WHERE tx_hash IS NOT NULL
        ON CONFLICT (asset, tx_hash) WHERE tx_hash IS NOT NULL
        DO UPDATE SET
            d = EXCLUDED.d,
            category = EXCLUDED.category,
            subcategory = EXCLUDED.subcategory,
            amount_eur = EXCLUDED.amount_eur,
            kind = EXCLUDED.kind
    """, (d, category, subcategory, asset, amount_eur, kind, tx_hash))
