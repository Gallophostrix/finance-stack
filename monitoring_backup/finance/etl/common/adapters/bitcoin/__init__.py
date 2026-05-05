# etl/common/adapters/bitcoin/__init__.py
"""
Bitcoin adapter
Exposes only the public ETL entry points: fetch_balances and detect_flows
"""

from .balances import fetch_balances
from .flows import detect_flows

__all__ = ["fetch_balances", "detect_flows"]