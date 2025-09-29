import datetime as dt
from dateutil.relativedelta import relativedelta

def start_of_month(d: dt.date) -> dt.date:
    return d.replace(day=1)
