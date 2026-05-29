"""
Sync parsed DimFile specs into the database.
Upserts providers, assets, accounts.
No parsing logic here — pure DB operations.
"""

import logging
from dataclasses import dataclass

from psycopg import Connection as PGConnection

from etl.dims.parser import AccountSpec, AssetSpec, DimFile, ProviderSpec

log = logging.getLogger("root")


@dataclass
class SyncResult:
    providers_upserted: int = 0
    assets_upserted: int = 0
    accounts_upserted: int = 0


def _upsert_provider(conn: PGConnection, provider: ProviderSpec) -> int:
    sql = """
    INSERT INTO core.providers (provider_type, provider_name)
    VALUES (%s, %s)
    ON CONFLICT (provider_type, provider_name) DO UPDATE
      SET provider_type = EXCLUDED.provider_type
    RETURNING provider_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (provider.type, provider.name))
        row = cur.fetchone()
    return row[0]


def _upsert_asset(conn: PGConnection, asset: AssetSpec) -> None:
    sql = """
    INSERT INTO core.assets (asset_code, class, decimals, coingecko_id, is_active)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (asset_code) DO UPDATE
      SET class        = EXCLUDED.class,
          decimals     = EXCLUDED.decimals,
          coingecko_id = EXCLUDED.coingecko_id,
          is_active    = EXCLUDED.is_active
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                asset.code,
                asset.asset_class,
                asset.decimals,
                asset.coingecko_id,
                asset.is_active,
            ),
        )


def _upsert_account(
    conn: PGConnection,
    account: AccountSpec,
    provider_id: int,
) -> None:
    sql = """
    INSERT INTO core.accounts
      (provider_id, account_type, external_identifier, label, "group")
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (provider_id, account_type, external_identifier) DO UPDATE
      SET label    = EXCLUDED.label,
          "group"  = EXCLUDED."group"
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                provider_id,
                account.account_type,
                account.external_id,
                account.label,
                account.group,
            ),
        )


def sync_dim_file(conn: PGConnection, dim: DimFile) -> SyncResult:
    """
    Sync one DimFile into the DB within a single transaction.
    Rolls back and logs on any error.
    """
    result = SyncResult()
    try:
        provider_id = _upsert_provider(conn, dim.provider)
        result.providers_upserted += 1
        log.info(
            "provider_synced", extra={"provider": dim.provider.name, "id": provider_id}
        )

        for asset in dim.assets:
            _upsert_asset(conn, asset)
            result.assets_upserted += 1
            log.info(
                "asset_synced",
                extra={
                    "asset": asset.code,
                    "class": asset.asset_class,
                    "active": asset.is_active,
                },
            )

        for account in dim.accounts:
            _upsert_account(conn, account, provider_id)
            result.accounts_upserted += 1
            log.info(
                "account_synced",
                extra={
                    "label": account.label,
                    "type": account.account_type,
                    "group": account.group,
                },
            )

        conn.commit()
        log.info(
            "dim_file_synced",
            extra={
                "file": dim.source_file.name,
                "providers": result.providers_upserted,
                "assets": result.assets_upserted,
                "accounts": result.accounts_upserted,
            },
        )

    except Exception as e:
        conn.rollback()
        log.error(
            "dim_file_sync_failed",
            extra={"file": dim.source_file.name, "error": str(e)},
        )
        raise

    return result


def sync_all(conn: PGConnection, dims: list[DimFile]) -> SyncResult:
    """
    Sync all DimFiles. Each file is its own transaction.
    Returns aggregated SyncResult.
    """
    total = SyncResult()
    for dim in dims:
        result = sync_dim_file(conn, dim)
        total.providers_upserted += result.providers_upserted
        total.assets_upserted += result.assets_upserted
        total.accounts_upserted += result.accounts_upserted

    log.info(
        "sync_all_done",
        extra={
            "providers": total.providers_upserted,
            "assets": total.assets_upserted,
            "accounts": total.accounts_upserted,
        },
    )
    return total
