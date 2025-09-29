import datetime as dt
from dateutil.relativedelta import relativedelta

def end_of_month(d: dt.date) -> dt.date:
    first = d.replace(day=1)
    return (first + relativedelta(months=1) - dt.timedelta(days=1))
