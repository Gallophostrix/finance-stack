# finance/etl/utils/db.py
from __future__ import annotations
import os, time
from typing import Optional
import psycopg2 # type: ignore
from psycopg2.extensions import connection as PGConnection # type: ignore

def connect_with_retry(
        dsn: Optional[str] = None,
        attempts: int = 8,
        base_sleep: float = 0.5
    ) -> PGConnection:
    """
    Connects to PostgreSQL with exponential backoff retries.
    Uses the provided DSN or the PG_DSN environment variable.
    """
    dsn_final = dsn or os.environ.get("PG_DSN")
    if not dsn_final:
        raise RuntimeError("PG_DSN not set")

    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return psycopg2.connect(dsn_final)
        except Exception as e:
            last_exc = e
            time.sleep(base_sleep * (2 ** i))
    raise last_exc or RuntimeError("connect_with_retry failed")