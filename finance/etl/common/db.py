"""
PostgreSQL connection with retry and DSN resolution.
"""

import logging
import os
import time
from typing import Optional

import psycopg
from psycopg import Connection as PGConnection

log = logging.getLogger("root")

_MAX_RETRIES = 5
_RETRY_DELAY = 2.0  # seconds


def resolve_dsn() -> str:
    """
    DSN resolution order:
    1. PG_DSN env var (full DSN string)
    2. DB_PASS_FILE env var (path to file containing password)
    Raises RuntimeError if neither is set.
    """
    dsn = os.environ.get("PG_DSN")
    if dsn:
        return dsn

    pwd_file = os.environ.get("DB_PASS_FILE")
    if pwd_file:
        try:
            pwd = open(pwd_file).read().strip()
        except OSError as e:
            raise RuntimeError(f"Cannot read DB_PASS_FILE={pwd_file}: {e}") from e
        host = os.environ.get("PG_HOST", "finance-db")
        port = os.environ.get("PG_PORT", "5432")
        user = os.environ.get("PG_USER", "finance")
        db = os.environ.get("PG_DB", "finance")
        return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

    raise RuntimeError(
        "No DB credentials found. Set PG_DSN or DB_PASS_FILE environment variable."
    )


def connect(dsn: Optional[str] = None) -> PGConnection:
    """
    Connect to PostgreSQL with exponential retry.
    Raises psycopg.OperationalError after max retries.
    """
    dsn = dsn or resolve_dsn()
    last_err: Optional[Exception] = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            conn = psycopg.connect(dsn)
            conn.autocommit = False
            log.info("db_connected", extra={"attempt": attempt})
            return conn
        except psycopg.OperationalError as e:
            last_err = e
            log.warning(
                "db_connect_failed",
                extra={"attempt": attempt, "max": _MAX_RETRIES, "error": str(e)},
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)

    raise psycopg.OperationalError(
        f"Failed to connect after {_MAX_RETRIES} attempts"
    ) from last_err
