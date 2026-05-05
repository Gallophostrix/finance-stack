# etl/common/loaders/dims/sql.py
from __future__ import annotations
from psycopg2.extensions import cursor # type: ignore

from .parsers import ProviderSpec, AssetSpec, AccountSpec, finalize_asset_decimals

def upsert_provider(cur: cursor, spec: ProviderSpec) -> int:
    """
    Upsert into core.providers and return provider_id.
    Uniqueness: (provider_type, provider_name).
    """
    cur.execute(
        """
        INSERT INTO core.providers (provider_type, provider_name)
        VALUES (%s, %s)
        ON CONFLICT (provider_type, provider_name) DO UPDATE
          SET provider_name = EXCLUDED.provider_name
        RETURNING provider_id;
        """,
        (spec.provider_type, spec.provider_name),
    )
    res = cur.fetchone()
    assert res is not None, "INSERT ... RETURNING failed to return a row"
    (provider_id,) = res
    return provider_id

def upsert_asset(cur: cursor, spec: AssetSpec) -> None:
    """
    Upsert asset by asset_code, with light metadata updates.
    """
    cur.execute(
        """
        INSERT INTO core.assets (asset_code, class, decimals, coingecko_id, is_active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (asset_code) DO UPDATE
          SET class        = EXCLUDED.class,
              decimals     = EXCLUDED.decimals,
              coingecko_id = COALESCE(EXCLUDED.coingecko_id, core.assets.coingecko_id),
              is_active    = EXCLUDED.is_active;
        """,
        (
            spec.asset_code,
            spec.asset_class,
            finalize_asset_decimals(spec),
            spec.coingecko_id,
            spec.is_active
        )
    )

def upsert_account(cur: cursor, provider_id: int, spec: AccountSpec) -> int:
    """
    Upsert account by (provider_id, account_type, external_identifier), update group/label/is_active.
    """
    cur.execute(
        """
        INSERT INTO core.accounts (provider_id, account_type, external_identifier, label, "group", is_active)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (provider_id, account_type, external_identifier) DO UPDATE
          SET label     = COALESCE(EXCLUDED.label, core.accounts.label),
              "group"   = EXCLUDED."group",
              is_active = EXCLUDED.is_active
        RETURNING account_id;
        """,
        (
            provider_id,
            spec.account_type,
            spec.external_identifier,
            spec.label,
            spec.group,
            spec.is_active
        )
    )
    res = cur.fetchone()
    assert res is not None, "INSERT ... RETURNING failed to return a row"
    (account_id,) = res
    return account_id
