# finance/etl/utils/db.py
import os, time, psycopg2
from pathlib import Path

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

def connect_with_retry(dsn: str, attempts: int = 8, base_sleep: float = 0.5):
    last = None
    for i in range(attempts):
        try:
            return psycopg2.connect(dsn)
        except Exception as e:
            last = e
            time.sleep(base_sleep * (2 ** i))  # backoff expo
    raise last
