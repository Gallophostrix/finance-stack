# finance/etl/utils/db.py
from __future__ import annotations
import os, time
from pathlib import Path
from typing import Optional
import psycopg2 # type: ignore
from psycopg2.extensions import connection as PGConnection # type: ignore

def read_db_pass() -> str:
    pass_file = os.environ.get("DB_PASS_FILE")
    if not pass_file:
        raise RuntimeError("DB_PASS_FILE not set")
    return Path(pass_file).read_text().strip()

def make_dsn_from_env() -> str:
    host = os.environ.get("DB_HOST", "finance-db")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "finance")
    user = os.environ.get("DB_USER", "finance")
    pwd  = read_db_pass()
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"

def connect_with_retry(
        dsn: str,
        attempts: int = 8,
        base_sleep: float = 0.5
    ) -> PGConnection:
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return psycopg2.connect(dsn)
        except Exception as e:
            last_exc = e
            time.sleep(base_sleep * (2 ** i))  # backoff expo
    # If we exhausted all attempts, raise the last exception
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("connect_with_retry failed without exception context")