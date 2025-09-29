# finance/etl/utils/dates.py
from datetime import datetime, timezone, date

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def today_utc_date() -> date:
    return datetime.now(timezone.utc).date()
