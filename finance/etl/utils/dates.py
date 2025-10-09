# finance/etl/utils/dates.py
from datetime import datetime, timezone, date

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def today_utc_date() -> date:
    return datetime.now(timezone.utc).date()

def last_month_utc_date() -> date:
    today = today_utc_date()
    year = today.year
    month = today.month - 1
    if month == 0:
        month = 12
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc).date()