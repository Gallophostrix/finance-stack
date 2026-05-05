# etl/common/adapters/__init__.py
from .bitcoin import fetch_balances as btc_fetch_balances, detect_flows as btc_detect_flows
from .cardano import fetch_balances as ada_fetch_balances, detect_flows as ada_detect_flows

__all__ = [
    "btc_fetch_balances",
    "btc_detect_flows",
    "ada_fetch_balances",
    "ada_detect_flows",
]
